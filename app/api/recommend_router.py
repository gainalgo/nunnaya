# ============================================================
# File: app/api/recommend_router.py
# Autocoin OS v3-H — Strategy Recommendation API
# ============================================================

from __future__ import annotations

from fastapi import APIRouter, Request, Query, HTTPException
from typing import Any, Dict, List, Tuple, Optional, Union
from datetime import datetime, timezone, timedelta
from pathlib import Path
from types import SimpleNamespace
import json
import os
import re
import threading

from app.strategy.strategy_recommender import StrategyRecommender

import logging
logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/recommend", tags=["recommend"])

KST = timezone(timedelta(hours=9))
SNAPSHOT_PATH = Path("runtime/recommend_snapshot.json")
SNAPSHOT_LOCK = threading.Lock()
DEFAULT_STRATEGIES = [
    "PINGPONG",
    "AUTOLOOP",
    "LADDER",
    "LIGHTNING",
    "GAZUA",
    "CONTRARIAN",
    "SNIPER",
]

def _parse_basis_kst(basis_kst: str) -> Tuple[int, int, str]:
    raw = (basis_kst or "").strip()
    m = re.match(r"^(\d{1,2}):(\d{2})$", raw)
    if not m:
        raise ValueError("basis_kst must be HH:MM")
    hour = int(m.group(1))
    minute = int(m.group(2))
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        raise ValueError("basis_kst out of range")
    return hour, minute, f"{hour:02d}:{minute:02d}"

def _resolve_snapshot_date(now_kst: datetime, hour: int, minute: int) -> str:
    basis = now_kst.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if now_kst < basis:
        basis = basis - timedelta(days=1)
    return basis.date().isoformat()

def _load_snapshot_store() -> Dict[str, Any]:
    if not SNAPSHOT_PATH.exists():
        return {"version": 1, "snapshots": {}}
    try:
        with SNAPSHOT_PATH.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {"version": 1, "snapshots": {}}
        data.setdefault("version", 1)
        data.setdefault("snapshots", {})
        return data
    except (OSError, json.JSONDecodeError, KeyError, AttributeError, TypeError, ValueError):
        logger.warning("recommend_router._load_snapshot_store L71 except", exc_info=True)
        return {"version": 1, "snapshots": {}}

def _save_snapshot_store(data: Dict[str, Any]) -> None:
    from app.core.io_utils import safe_write_json
    safe_write_json(str(SNAPSHOT_PATH), data, ensure_ascii=True)

def _score_item(item: Dict[str, Any]) -> float:
    try:
        return float(item.get("ai_adjusted_score") or item.get("ai_score") or 0.0)
    except (KeyError, AttributeError, TypeError, ValueError):
        logger.warning("recommend_router._score_item L86 except", exc_info=True)
        return 0.0

def _fetch_strategy_items(system: Any, strategy: str, n: int) -> List[Dict[str, Any]]:
    from app.api import strategy_recommend_router
    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(system=system)))
    data = strategy_recommend_router.get_rich_recommendations(req, strategy=strategy, n=n)
    items = data.get("items") or []
    results: List[Dict[str, Any]] = []
    for item in items:
        if not item or not item.get("market"):
            continue
        cloned = dict(item)
        cloned["strategy"] = strategy
        results.append(cloned)
    return results

def _normalize_strategies(strategies: Optional[Union[str, List[str]]]) -> List[str]:
    if not strategies:
        return DEFAULT_STRATEGIES[:]
    if isinstance(strategies, str):
        st_list = [s.strip().upper() for s in strategies.split(",") if s.strip()]
    else:
        st_list = [str(s).strip().upper() for s in strategies if str(s).strip()]
    return st_list

def compute_snapshot(
    system: Any,
    *,
    basis_kst: str = "07:00",
    n: int = 5,
    strategies: Optional[Union[str, List[str]]] = None,
    force: bool = False,
    now_kst: Optional[datetime] = None,
) -> Dict[str, Any]:
    hour, minute, basis_norm = _parse_basis_kst(basis_kst)

    try:
        n = int(n)
    except (TypeError, ValueError):
        logger.warning("recommend_router.compute_snapshot L128 except", exc_info=True)
        n = 5
    n = max(1, min(20, n))

    st_list = _normalize_strategies(strategies)
    if not st_list:
        raise ValueError("strategies empty")

    now_kst = now_kst or datetime.now(KST)
    snapshot_date = _resolve_snapshot_date(now_kst, hour, minute)
    key = f"{snapshot_date}T{basis_norm}"

    with SNAPSHOT_LOCK:
        store = _load_snapshot_store()
        snapshots = store.get("snapshots") or {}
        cached = snapshots.get(key)
        if cached and not force:
            return {"ok": True, "cached": True, **cached}

        per_strategy_n = max(n, 5)
        all_items: List[Dict[str, Any]] = []
        for st in st_list:
            try:
                all_items.extend(_fetch_strategy_items(system, st, per_strategy_n))
            except (AttributeError, TypeError) as exc:
                logger.warning("[recommend_router] %s: %s", 'recommend_router.compute_snapshot except-> continue', exc, exc_info=True)
                continue

        all_items.sort(key=_score_item, reverse=True)
        final_items: List[Dict[str, Any]] = []
        seen = set()
        for item in all_items:
            mkt = item.get("market")
            if not mkt or mkt in seen:
                continue
            seen.add(mkt)
            item["rank_score"] = round(_score_item(item), 6)
            final_items.append(item)
            if len(final_items) >= n:
                break

        payload = {
            "basis_kst": basis_norm,
            "snapshot_date": snapshot_date,
            "created_at_kst": now_kst.strftime("%Y-%m-%d %H:%M:%S"),
            "asof_kst": now_kst.strftime("%Y-%m-%d %H:%M:%S"),
            "n": n,
            "strategies": st_list,
            "items": final_items,
        }

        store["snapshots"] = snapshots
        store["snapshots"][key] = payload
        _save_snapshot_store(store)

        return {"ok": True, "cached": False, **payload}

@router.get("/strategy", summary="Get recommended params for a strategy")
def recommend_strategy(
    request: Request,
    strategy: str = Query(..., description="Strategy name (e.g., LADDER, GAZUA)"),
    market: str = Query(..., description="Market code (e.g., BTCUSDT)"),
    budget_usdt: float = Query(0, description="Current budget (optional)"),
) -> Dict[str, Any]:
    system = request.app.state.system
    try:
        rec = StrategyRecommender(system).recommend(
            strategy=strategy,
            market=market,
            budget_usdt=budget_usdt if budget_usdt > 0 else None,
        )
        return {"ok": True, **rec}
    except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
        logger.warning("recommend_router.recommend_strategy L201: %s", exc)
        raise HTTPException(status_code=400, detail={"ok": False, "error": str(exc)})

@router.get("/snapshot", summary="Get daily reserved snapshot (KST basis)")
def recommend_snapshot(
    request: Request,
    basis_kst: str = Query("07:00", description="Basis time in KST (HH:MM)"),
    n: int = Query(5, ge=1, le=20, description="Number of coins"),
    strategies: str = Query("", description="Comma-separated strategies (optional)"),
    force: int = Query(0, description="Force recompute (1/0)"),
) -> Dict[str, Any]:
    system = request.app.state.system
    try:
        return compute_snapshot(
            system,
            basis_kst=basis_kst,
            n=n,
            strategies=strategies or None,
            force=bool(force),
        )
    except ValueError as exc:
        logger.warning("recommend_router.recommend_snapshot L222: %s", exc)
        raise HTTPException(status_code=400, detail={"ok": False, "error": str(exc)})
    except (AttributeError, TypeError, ValueError) as exc:
        logger.warning("recommend_router.recommend_snapshot L224: %s", exc)
        raise HTTPException(status_code=500, detail={"ok": False, "error": str(exc)})
