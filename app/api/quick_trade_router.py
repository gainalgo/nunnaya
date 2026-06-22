"""Quick Trade API Router

수동 즉시/조건부 거래 API
"""

from fastapi import APIRouter, Request
from typing import Any, Dict, Optional
from pydantic import BaseModel

import logging
import time as _time
from collections import defaultdict as _defaultdict

logger = logging.getLogger(__name__)

# ── [2026-04-09 보안] Rate Limiting ──────────────────────────
_RATE_LIMIT: Dict[str, list] = _defaultdict(list)
_RATE_LIMIT_MAX = 20
_RATE_LIMIT_WINDOW = 60


def _check_rate_limit(request: Request) -> bool:
    ip = request.client.host if request.client else "unknown"
    now = _time.time()
    window = now - _RATE_LIMIT_WINDOW
    _RATE_LIMIT[ip] = [t for t in _RATE_LIMIT[ip] if t > window]
    if len(_RATE_LIMIT[ip]) >= _RATE_LIMIT_MAX:
        return False
    _RATE_LIMIT[ip].append(now)
    return True


router = APIRouter(prefix="/api/trade", tags=["quick_trade"])

class QuickTradeRequest(BaseModel):
    """Quick Trade 요청 스키마"""
    exchange: str = "bybit"  # bybit
    market_input: str        # BTC, BTCUSDT 등
    side: str                # buy | sell
    
    amount_mode: str = "quote"   # quote | percent
    amount_value: float = 0.0
    
    mode: str = "immediate"      # immediate | conditional
    guard_policy: str = "global"  # global | entry_limit_only | force
    
    conditional: Optional[Dict[str, Any]] = None
    execution: Optional[Dict[str, Any]] = None

class CancelRequest(BaseModel):
    """취소 요청"""
    quick_id: str

# =============================================
# 정적 경로 (동적 경로보다 먼저 정의해야 함)
# =============================================

@router.post("/quick")
def quick_trade_submit(request: Request, body: QuickTradeRequest) -> Dict[str, Any]:
    """Quick Trade 주문 제출"""
    if not _check_rate_limit(request):
        return {"ok": False, "error": "rate_limited", "message": "Too many requests. Max 20/min."}
    system = request.app.state.system
    manager = getattr(system, "quick_trade_manager", None)

    if not manager:
        return {"ok": False, "error": "QuickTradeManager not available"}
    
    return manager.submit(body.model_dump())

@router.get("/quick/pending/list")
def quick_trade_pending_list(request: Request) -> Dict[str, Any]:
    """펜딩 주문 목록"""
    system = request.app.state.system
    manager = getattr(system, "quick_trade_manager", None)
    
    if not manager:
        return {"ok": False, "error": "QuickTradeManager not available"}
    
    return {"ok": True, "orders": manager.get_pending_orders()}

@router.post("/quick/conditional")
def quick_trade_conditional(request: Request, body: Dict[str, Any]) -> Dict[str, Any]:
    """조건부 주문 등록 (V2 대시보드용 래퍼)

    지원 조건:
    - above/below: 지정가 도달 시 체결
    - near_low: N분 내 최저가 근처 도달 시 매수
    - near_high: N분 내 최고가 근처 도달 시 매도
    """
    if not _check_rate_limit(request):
        return {"ok": False, "error": "rate_limited", "message": "Too many requests. Max 20/min."}
    system = request.app.state.system
    manager = getattr(system, "quick_trade_manager", None)

    if not manager:
        return {"ok": False, "error": "QuickTradeManager not available"}

    market = body.get("market", "")
    side = body.get("side", "buy")
    amount_usdt = float(body.get("amount_usdt", 0))
    condition = body.get("condition", "above")

    if not market:
        return {"ok": False, "error": "market required"}
    # [2026-04-09 보안] 입력값 범위 검증
    if amount_usdt < 0 or amount_usdt > 100000:
        return {"ok": False, "error": "amount_usdt must be 0~100000"}
    if side not in ("buy", "sell"):
        return {"ok": False, "error": "side must be 'buy' or 'sell'"}
    
    # 지정가 조건 (above/below)
    if condition in ("above", "below"):
        target_price = float(body.get("target_price", 0))
        if not target_price:
            return {"ok": False, "error": "target_price required"}
        
        req = {
            "exchange": "bybit",
            "market_input": market,
            "side": side,
            "amount_mode": "quote",
            "amount_value": amount_usdt,
            "mode": "conditional",
            "guard_policy": "global",
            "conditional": {
                "trigger": "price_above" if condition == "above" else "price_below",
                "target_price": target_price,
            },
        }
    # 최저가/최고가 조건 (near_low/near_high)
    elif condition in ("near_low", "near_high"):
        lookback = int(body.get("lookback_minutes", 15))
        threshold = float(body.get("threshold_pct", 0.3))
        expiry = int(body.get("expiry_minutes", 30))
        
        req = {
            "exchange": "bybit",
            "market_input": market,
            "side": side,
            "amount_mode": "quote",
            "amount_value": amount_usdt,
            "mode": "conditional",
            "guard_policy": "global",
            "conditional": {
                "trigger": condition,
                "lookback_minutes": lookback,
                "threshold_pct": threshold,
                "expiry_minutes": expiry,
            },
        }
    else:
        return {"ok": False, "error": f"Unknown condition: {condition}"}
    
    return manager.submit(req)

@router.get("/quick/estimate")
def quick_trade_estimate(
    request: Request, 
    market: str = "", 
    lookback_min: int = 15,
    threshold_mode: str = "pct",
    threshold_value: float = 0.2,
    trigger: str = "near_low"
) -> Dict[str, Any]:
    """조건부 주문 예상 진입가 계산 (Bybit V5 kline API)"""
    from app.core.rate_limiter import bybit_get
    from app.core.constants import (
        BYBIT_MARKET_KLINE,
        BYBIT_MARKET_TICKERS,
        bybit_v5_rest_category,
        parse_bybit_list,
        normalize_bybit_ticker,
    )
    from app.core.currency import Q

    if not market:
        return {"ok": False, "error": "market required"}

    # 마켓 정규화
    market = Q.normalize(market)

    try:
        # 1분 캔들로 N분 데이터 조회
        candle_count = min(lookback_min, 200)
        resp = bybit_get(BYBIT_MARKET_KLINE, params={"category": bybit_v5_rest_category(), "symbol": market, "interval": "1", "limit": candle_count}, timeout=5)
        resp.raise_for_status()
        raw_candles = parse_bybit_list(resp.json())
        candles = [{"low_price": float(k[3]), "high_price": float(k[2]), "trade_price": float(k[4])} for k in raw_candles if isinstance(k, (list, tuple)) and len(k) >= 5]

        if not candles or len(candles) < 3:
            return {"ok": False, "error": "Insufficient candle data"}

        lows = [float(c.get("low_price", 0)) for c in candles if c.get("low_price")]
        highs = [float(c.get("high_price", 0)) for c in candles if c.get("high_price")]

        if not lows or not highs:
            return {"ok": False, "error": "Invalid candle data"}

        low = min(lows)
        high = max(highs)

        # 현재가 조회
        ticker_resp = bybit_get(BYBIT_MARKET_TICKERS, params={"category": bybit_v5_rest_category()}, timeout=5)
        ticker_resp.raise_for_status()
        current_price = 0.0
        for _t in parse_bybit_list(ticker_resp.json()):
            if isinstance(_t, dict):
                _tc = normalize_bybit_ticker(_t)
                if _tc.get("market", "").upper() == market.upper():
                    current_price = float(_tc.get("trade_price", 0))
                    break

        if current_price <= 0:
            return {"ok": False, "error": "Current price not available"}
        
        # 예상 진입가 계산
        if trigger == "near_low":
            if threshold_mode == "pct":
                entry_price = low * (1 + threshold_value / 100.0)
            else:  # quote
                entry_price = low + threshold_value
            reference = low
        else:  # near_high
            if threshold_mode == "pct":
                entry_price = high * (1 - threshold_value / 100.0)
            else:
                entry_price = high - threshold_value
            reference = high
        
        # 현재가 대비 차이
        diff_from_current = entry_price - current_price
        diff_pct = (diff_from_current / current_price) * 100 if current_price else 0
        
        return {
            "ok": True,
            "market": market,
            "current_price": current_price,
            "low": low,
            "high": high,
            "lookback_min": lookback_min,
            "trigger": trigger,
            "threshold_mode": threshold_mode,
            "threshold_value": threshold_value,
            "entry_price": round(entry_price, 2),
            "reference_price": reference,
            "diff_from_current": round(diff_from_current, 2),
            "diff_pct": round(diff_pct, 2),
        }
    except (ConnectionError, TimeoutError, OSError) as e:
        logger.warning("quick_trade_router.quick_trade_estimate L223: %s", e)
        return {"ok": False, "error": f"API error: {str(e)}"}

@router.get("/markets/suggest")
def markets_suggest(request: Request, query: str = "", limit: int = 20) -> Dict[str, Any]:
    """마켓 자동완성 제안"""
    system = request.app.state.system
    
    # 현재 등록된 마켓 + 거래소 전체 마켓에서 검색
    results = []
    query_upper = query.strip().upper()
    
    if not query_upper:
        return {"ok": True, "markets": []}
    
    # 1. 활성 마켓에서 검색
    try:
        price_store = getattr(system, "price_store", None)
        if price_store:
            all_prices = price_store.get_all()
            for market in all_prices.keys():
                if query_upper in market.upper():
                    base = market.split("-")[1] if "-" in market else market
                    quote = market.split("-")[0] if "-" in market else "USDT"
                    results.append({
                        "market": market,
                        "base": base,
                        "quote": quote,
                        "exchange": "bybit",
                        "active": True,
                    })
    except (KeyError, IndexError, AttributeError, TypeError) as exc:
        logger.warning("[quick_trade_router] %s: %s", '1. 활성 마켓에서 검색', exc, exc_info=True)
    
    # 2. 거래소 전체 마켓에서 검색 (캐시된 데이터 사용)
    try:
        # bybit 마켓 검색
        from app.integrations.bybit_markets import fetch_bybit_markets, filter_quote_markets as filter_quote_markets
        all_markets = filter_quote_markets(fetch_bybit_markets())
        for m in all_markets:
            if query_upper in m.upper() and not any(r["market"] == m for r in results):
                base = m.split("-")[1] if "-" in m else m
                quote = m.split("-")[0] if "-" in m else "USDT"
                results.append({
                    "market": m,
                    "base": base,
                    "quote": quote,
                    "exchange": "bybit",
                    "active": False,
                })
                if len(results) >= limit:
                    break
    except (KeyError, IndexError, AttributeError, TypeError, ValueError) as exc:
        logger.warning("[quick_trade_router] %s: %s", 'bybit 마켓 검색', exc, exc_info=True)
    
    return {"ok": True, "markets": results[:limit]}

@router.post("/markets/resolve")
def markets_resolve(request: Request, body: Dict[str, Any]) -> Dict[str, Any]:
    """마켓 입력값 정규화"""
    system = request.app.state.system
    manager = getattr(system, "quick_trade_manager", None)
    
    if not manager:
        return {"ok": False, "error": "QuickTradeManager not available"}
    
    market_input = body.get("input", "")
    quote = body.get("preferred_quote", "USDT")

    resolved = manager.resolve_market(market_input, quote)

    if resolved:
        base = resolved.split("-")[1] if "-" in resolved else resolved
        return {
            "ok": True,
            "market": resolved,
            "base": base,
            "quote": quote,
            "exchange": "bybit",
        }
    
    return {"ok": False, "error": "Cannot resolve market"}

# =============================================
# 동적 경로 (정적 경로보다 뒤에 정의)
# =============================================

@router.get("/quick/{quick_id}")
def quick_trade_get(request: Request, quick_id: str) -> Dict[str, Any]:
    """Quick Trade 주문 조회"""
    system = request.app.state.system
    manager = getattr(system, "quick_trade_manager", None)
    
    if not manager:
        return {"ok": False, "error": "QuickTradeManager not available"}
    
    order = manager.get_order(quick_id)
    if order:
        return {"ok": True, "order": order}
    return {"ok": False, "error": "Order not found"}

@router.post("/quick/{quick_id}/cancel")
def quick_trade_cancel(request: Request, quick_id: str) -> Dict[str, Any]:
    """Quick Trade 주문 취소"""
    system = request.app.state.system
    manager = getattr(system, "quick_trade_manager", None)
    
    if not manager:
        return {"ok": False, "error": "QuickTradeManager not available"}
    
    return manager.cancel(quick_id)
