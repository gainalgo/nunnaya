# ============================================================
# File: app/manager/am_performance.py
# Autocoin OS v3-H — Auto/Manual Performance Report
# ============================================================

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

from app.manager.ledger_pnl import aggregate_fill_pnl, MarketFillAgg

AUTO_HINTS = [
    "autopilot",
    "autoapprove",
    "reserved_approve",
    "strategy_recommendations",
    "perf_rebalance",
    "graduation",
    "signal_miss",
    "idle",
    "candidate",
    "queue",
]

MANUAL_HINTS = ["manual", "ui", "api", "debug"]

STRATEGY_KEYS = ["PINGPONG", "AUTOLOOP", "LADDER", "LIGHTNING", "GAZUA", "CONTRARIAN", "SNIPER"]

def _has_hint(reasons: List[str], tokens: List[str]) -> bool:
    if not reasons:
        return False
    low = [str(r).lower() for r in reasons if r]
    return any(any(t in r for t in tokens) for r in low)

def infer_auto_manual_mode(reasons: List[str], tune_mode: Optional[str]) -> str:
    if _has_hint(reasons, MANUAL_HINTS):
        return "M"
    if _has_hint(reasons, AUTO_HINTS):
        return "A"
    if tune_mode:
        return "A" if str(tune_mode).upper() == "AUTO" else "M"
    return "-"

def _get_ctx_attr(ctx: Any, key: str, default: Any = None) -> Any:
    if ctx is None:
        return default
    if isinstance(ctx, dict):
        return ctx.get(key, default)
    return getattr(ctx, key, default)

def _extract_tune_mode(ctx: Any) -> Optional[str]:
    controls = _get_ctx_attr(ctx, "controls")
    if not isinstance(controls, dict):
        return None
    strategy = controls.get("strategy") or {}
    params = strategy.get("params") or {}
    tune_mode = params.get("tune_mode")
    return str(tune_mode) if tune_mode is not None else None

def _extract_strategy_from_ctx(ctx: Any) -> str:
    if ctx is None:
        return ""
    strategy_state = _get_ctx_attr(ctx, "strategy_state") or _get_ctx_attr(ctx, "strategy")
    if isinstance(strategy_state, dict):
        mode = strategy_state.get("mode")
        if mode:
            return str(mode).upper()
    controls = _get_ctx_attr(ctx, "controls")
    if isinstance(controls, dict):
        strategy = controls.get("strategy") or {}
        mode = strategy.get("mode")
        if mode:
            return str(mode).upper()
    return ""

def _extract_strategy_from_reason(reason: Optional[str]) -> str:
    if not reason:
        return ""
    raw = str(reason)
    prefix = raw.split(":", 1)[0].upper()
    if prefix in STRATEGY_KEYS:
        return prefix
    upper = raw.upper()
    for key in STRATEGY_KEYS:
        if key in upper:
            return key
    return ""

def _strategy_map_from_records(records: List[Dict[str, Any]]) -> Dict[str, str]:
    counts: Dict[str, Dict[str, int]] = {}
    for rec in records:
        try:
            ev = rec.get("event")
            if ev not in ("FILL_BUY", "FILL_SELL", "FILL_SYNC_BUY", "FILL_SYNC_SELL"):
                continue
            market = rec.get("market") or (rec.get("data") or {}).get("market")
            if not market:
                continue
            data = rec.get("data") or {}
            reason = data.get("reason")
            strategy = _extract_strategy_from_reason(reason)
            if not strategy:
                continue
            bucket = counts.setdefault(str(market), {})
            bucket[strategy] = bucket.get(strategy, 0) + 1
        except (KeyError, AttributeError, TypeError) as exc:
            logger.warning("[am_performance] %s: %s", 'am_performance._strategy_map_from_records except-> continue', exc, exc_info=True)
            continue

    out: Dict[str, str] = {}
    for market, bucket in counts.items():
        top = sorted(bucket.items(), key=lambda x: x[1], reverse=True)
        out[market] = top[0][0] if top else ""
    return out

@dataclass
class ModeSummary:
    mode: str
    market_n: int = 0
    total_trades: int = 0
    total_pnl_usdt: float = 0.0
    total_invested_usdt: float = 0.0
    roi_pct: float = 0.0
    markets: List[str] = field(default_factory=list)

    def finalize(self) -> None:
        if self.total_invested_usdt > 0:
            self.roi_pct = (self.total_pnl_usdt / self.total_invested_usdt) * 100.0
        self.market_n = len(set(self.markets))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mode": self.mode,
            "market_n": int(self.market_n),
            "total_trades": int(self.total_trades),
            "total_pnl_usdt": round(self.total_pnl_usdt, 2),
            "total_invested_usdt": round(self.total_invested_usdt, 2),
            "roi_pct": round(self.roi_pct, 4),
            "markets": sorted(set(self.markets)),
        }

def build_auto_manual_report(
    system: Any,
    *,
    since_hours: float = 168.0,
    tail_lines: int = 50000,
) -> Dict[str, Any]:
    since_hours = float(since_hours or 0.0)
    if since_hours <= 0:
        since_hours = 168.0

    since_ts = time.time() - (since_hours * 3600.0)
    until_ts = time.time()

    records = system.ledger.tail_records(since_ts=since_ts, tail_lines=tail_lines)
    aggs = aggregate_fill_pnl(records, since_ts=since_ts, until_ts=until_ts)

    strategy_from_records = _strategy_map_from_records(records)

    oma_snapshot = {}
    try:
        oma_snapshot = system.oma.snapshot()
    except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError):
        logger.warning("[AMPerformance] oma.snapshot() failed", exc_info=True)
        oma_snapshot = {}

    reason_map: Dict[str, List[str]] = {}
    strategy_map: Dict[str, str] = {}
    for bucket in ("active", "watch", "recovery"):
        for row in oma_snapshot.get(bucket, []) or []:
            market = row.get("market")
            if not market:
                continue
            reason_map[str(market)] = list(row.get("reason") or [])
            st = row.get("strategy") or ""
            if st:
                strategy_map[str(market)] = str(st).upper()

    tune_mode_map: Dict[str, str] = {}
    ctx_strategy_map: Dict[str, str] = {}
    try:
        contexts = getattr(system, "coordinator", None)
        ctx_map = getattr(contexts, "contexts", {}) if contexts else {}
        if isinstance(ctx_map, dict):
            for market, ctx in ctx_map.items():
                tune = _extract_tune_mode(ctx)
                if tune:
                    tune_mode_map[str(market)] = tune
                st = _extract_strategy_from_ctx(ctx)
                if st:
                    ctx_strategy_map[str(market)] = st
    except (KeyError, AttributeError, TypeError) as exc:
        logger.warning("[am_performance] %s: %s", 'am_performance fallback', exc, exc_info=True)

    summary = {
        "A": ModeSummary(mode="A"),
        "M": ModeSummary(mode="M"),
        "-": ModeSummary(mode="-"),
    }
    by_strategy: Dict[str, Dict[str, ModeSummary]] = {}

    items: List[Dict[str, Any]] = []
    for market, agg in aggs.items():
        reasons = reason_map.get(market, [])
        tune_mode = tune_mode_map.get(market)
        mode = infer_auto_manual_mode(reasons, tune_mode)

        strategy = (
            strategy_map.get(market)
            or ctx_strategy_map.get(market)
            or strategy_from_records.get(market, "")
        )

        roi_pct = (agg.net_cash_usdt / agg.buy_funds_usdt * 100.0) if agg.buy_funds_usdt > 0 else 0.0

        items.append({
            "market": market,
            "mode": mode,
            "strategy": strategy,
            "trade_n": agg.trade_n,
            "buy_n": agg.buy_n,
            "sell_n": agg.sell_n,
            "total_pnl_usdt": round(agg.net_cash_usdt, 2),
            "total_invested_usdt": round(agg.buy_funds_usdt, 2),
            "roi_pct": round(roi_pct, 4),
            "first_ts": agg.first_ts,
            "last_ts": agg.last_ts,
        })

        bucket = summary.get(mode, summary["-"])
        bucket.total_trades += agg.trade_n
        bucket.total_pnl_usdt += agg.net_cash_usdt
        bucket.total_invested_usdt += agg.buy_funds_usdt
        bucket.markets.append(market)

        strat_key = strategy or "UNKNOWN"
        strat_bucket = by_strategy.setdefault(
            strat_key,
            {"A": ModeSummary(mode="A"), "M": ModeSummary(mode="M"), "-": ModeSummary(mode="-")},
        )
        sb = strat_bucket.get(mode, strat_bucket["-"])
        sb.total_trades += agg.trade_n
        sb.total_pnl_usdt += agg.net_cash_usdt
        sb.total_invested_usdt += agg.buy_funds_usdt
        sb.markets.append(market)

    for bucket in summary.values():
        bucket.finalize()

    for strat_bucket in by_strategy.values():
        for sb in strat_bucket.values():
            sb.finalize()

    items.sort(key=lambda x: x.get("total_pnl_usdt", 0.0), reverse=True)

    return {
        "ok": True,
        "since_hours": since_hours,
        "since_ts": since_ts,
        "until_ts": until_ts,
        "summary": {k: v.to_dict() for k, v in summary.items()},
        "by_strategy": {
            strat: {mode: bucket.to_dict() for mode, bucket in buckets.items()}
            for strat, buckets in by_strategy.items()
        },
        "items": items,
        "mode_basis": "oma_reason + tune_mode (context)",
    }
