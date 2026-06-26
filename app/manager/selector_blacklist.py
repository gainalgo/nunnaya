from __future__ import annotations

import json
import logging
import os
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from threading import Lock
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set

logger = logging.getLogger(__name__)

def _now_ts() -> float:
    try:
        return float(time.time())
    except (TypeError, ValueError):
        logger.warning("[SelectorBlacklist] _now_ts: time.time() failed", exc_info=True)
        return 0.0

def _norm_market(market: str) -> str:
    return str(market or "").strip().upper()

def _bool_env(name: str, default: bool = False) -> bool:
    raw = str(os.getenv(name, "1" if default else "0")).strip().lower()
    return raw in ("1", "true", "yes", "y", "on")

@dataclass(frozen=True)
class SelectorBlacklistSignal:
    market: str
    reason: str
    ttl_sec: float
    source: str = "selector"
    meta: Dict[str, Any] = field(default_factory=dict)

def classify_liquidity_risk(
    *,
    price: float,
    vol24_usdt: float,
    spread_bps: float,
    bid_depth_usdt: float,
    target_exit_usdt: float,
    execution_penalty: float,
    base_spread_bps: float = 25.0,
) -> Optional[Dict[str, Any]]:
    ttl_sec = 0.0
    flags: List[str] = []
    soft_spread_bps = max(25.0, float(base_spread_bps or 25.0))
    medium_spread_bps = max(45.0, soft_spread_bps * 1.8)
    hard_spread_bps = max(80.0, soft_spread_bps * 3.0)

    if vol24_usdt < 500_000:
        ttl_sec = max(ttl_sec, 7 * 86400.0)
        flags.append("vol24_lt_500k")
    elif vol24_usdt < 1_000_000:
        ttl_sec = max(ttl_sec, 3 * 86400.0)
        flags.append("vol24_lt_1m")
    elif vol24_usdt < 3_000_000:
        ttl_sec = max(ttl_sec, 86400.0)
        flags.append("vol24_lt_3m")

    if spread_bps > hard_spread_bps:
        ttl_sec = max(ttl_sec, 7 * 86400.0)
        flags.append(f"spread_gt_{int(round(hard_spread_bps))}bps")
    elif spread_bps > medium_spread_bps:
        ttl_sec = max(ttl_sec, 3 * 86400.0)
        flags.append(f"spread_gt_{int(round(medium_spread_bps))}bps")
    elif spread_bps > soft_spread_bps:
        ttl_sec = max(ttl_sec, 86400.0)
        flags.append(f"spread_gt_{int(round(soft_spread_bps))}bps")

    if target_exit_usdt > 0.0 and bid_depth_usdt > 0.0:
        depth_ratio = float(bid_depth_usdt) / float(target_exit_usdt)
        if depth_ratio < 0.25:
            ttl_sec = max(ttl_sec, 7 * 86400.0)
            flags.append("bid_depth_lt_25pct")
        elif depth_ratio < 0.50:
            ttl_sec = max(ttl_sec, 3 * 86400.0)
            flags.append("bid_depth_lt_50pct")
        elif depth_ratio < 1.00:
            ttl_sec = max(ttl_sec, 86400.0)
            flags.append("bid_depth_lt_100pct")

    if execution_penalty <= -12.0:
        ttl_sec = max(ttl_sec, 7 * 86400.0)
        flags.append("eq_pen_hard")
    elif execution_penalty <= -8.0:
        ttl_sec = max(ttl_sec, 3 * 86400.0)
        flags.append("eq_pen_medium")
    elif execution_penalty <= -4.0:
        ttl_sec = max(ttl_sec, 86400.0)
        flags.append("eq_pen_soft")

    if ttl_sec <= 0.0:
        return None

    if ttl_sec >= 7 * 86400.0:
        reason = "liquidity_hard"
    elif ttl_sec >= 3 * 86400.0:
        reason = "liquidity_medium"
    else:
        reason = "liquidity_soft"

    return {
        "reason": reason,
        "ttl_sec": float(ttl_sec),
        "meta": {
            "price": float(price),
            "vol24_usdt": float(vol24_usdt),
            "spread_bps": float(spread_bps),
            "bid_depth_usdt": float(bid_depth_usdt),
            "target_exit_usdt": float(target_exit_usdt),
            "execution_penalty": float(execution_penalty),
            "flags": list(flags),
        },
    }

_LEDGER_CACHE: Dict[str, Any] = {
    "key": None,
    "signals": [],
}

def _iter_recent_ledger_rows(path: str, *, max_lines: int, cutoff_ts: float) -> Iterable[Dict[str, Any]]:
    max_lines = max(100, int(max_lines))
    buf: deque[str] = deque(maxlen=max_lines)
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            if line:
                buf.append(line)

    for line in buf:
        try:
            row = json.loads(line)
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            logger.warning("[selector_blacklist] %s: %s", 'selector_blacklist._iter_recent_ledger_rows except-> continue', exc, exc_info=True)
            continue
        if not isinstance(row, dict):
            continue
        try:
            ts = float(row.get("ts") or 0.0)
        except (KeyError, AttributeError, TypeError, ValueError):
            logger.warning("[SelectorBlacklist] _iter_recent: ts parse failed", exc_info=True)
            ts = 0.0
        if ts <= 0.0 or ts < cutoff_ts:
            continue
        yield row

def build_sell_difficulty_blacklist_signals(
    ledger_path: str,
    *,
    now_ts: Optional[float] = None,
    lookback_sec: float = 86400.0,
    max_lines: int = 8000,
) -> List[SelectorBlacklistSignal]:
    path = str(ledger_path or "").strip()
    if not path or not os.path.exists(path):
        return []

    now_ts = float(now_ts or _now_ts())
    lookback_sec = max(300.0, float(lookback_sec or 86400.0))
    max_lines = max(100, int(max_lines or 8000))

    try:
        st = os.stat(path)
        cache_key = (
            os.path.abspath(path),
            int(st.st_mtime),
            int(st.st_size),
            int(now_ts // 60),
            int(lookback_sec),
            int(max_lines),
        )
        if _LEDGER_CACHE.get("key") == cache_key:
            cached = _LEDGER_CACHE.get("signals") or []
            return list(cached)
    except (KeyError, AttributeError, TypeError, ValueError):
        logger.warning("[SelectorBlacklist] scan_ledger_for_signals: cache key build failed", exc_info=True)
        cache_key = None

    cutoff_ts = now_ts - lookback_sec
    counters: Dict[str, Counter[str]] = {}

    for row in _iter_recent_ledger_rows(path, max_lines=max_lines, cutoff_ts=cutoff_ts):
        market = _norm_market(row.get("market"))
        if not market:
            continue
        data = row.get("data") if isinstance(row.get("data"), dict) else {}
        event = str(row.get("event") or "").strip().upper()
        cause = str(data.get("cause") or "").strip().lower()
        side = str(data.get("side") or "").strip().lower()

        ctr = counters.setdefault(market, Counter())
        if event == "EXIT_BLOCKED" and cause == "order_pending":
            ctr["order_pending"] += 1
        elif event == "EXIT_BLOCKED" and cause == "profit_guard_streak":
            ctr["profit_guard_streak"] += 1
        elif event == "FILL_NONE" and side == "ask":
            ctr["fill_none_sell"] += 1
        elif event == "FORCE_SELL":
            ctr["force_sell"] += 1

    signals: List[SelectorBlacklistSignal] = []
    for market, ctr in counters.items():
        ttl_sec = 0.0
        reason = ""
        if ctr.get("force_sell", 0) >= 1:
            ttl_sec = 7 * 86400.0
            reason = "sell_difficulty_force_sell"
        elif ctr.get("fill_none_sell", 0) >= 1 or ctr.get("order_pending", 0) >= 10:
            ttl_sec = 3 * 86400.0
            reason = "sell_difficulty_hard"
        elif ctr.get("order_pending", 0) >= 3 or ctr.get("profit_guard_streak", 0) >= 6:
            ttl_sec = 86400.0
            reason = "sell_difficulty_medium"
        elif ctr.get("profit_guard_streak", 0) >= 2:
            ttl_sec = 6 * 3600.0
            reason = "sell_difficulty_soft"

        if ttl_sec <= 0.0 or not reason:
            continue

        signals.append(
            SelectorBlacklistSignal(
                market=market,
                reason=reason,
                ttl_sec=ttl_sec,
                source="ledger_exitability",
                meta={
                    "lookback_sec": float(lookback_sec),
                    "order_pending": int(ctr.get("order_pending", 0)),
                    "profit_guard_streak": int(ctr.get("profit_guard_streak", 0)),
                    "fill_none_sell": int(ctr.get("fill_none_sell", 0)),
                    "force_sell": int(ctr.get("force_sell", 0)),
                },
            )
        )

    if cache_key is not None:
        _LEDGER_CACHE["key"] = cache_key
        _LEDGER_CACHE["signals"] = list(signals)

    return signals

class SelectorBlacklistStore:
    def __init__(self, *, path: Optional[str] = None) -> None:
        self.path = path or os.getenv("OMA_SELECTOR_BLACKLIST_PATH", "runtime/selector_blacklist.json")
        self._lock = Lock()
        self._markets: Dict[str, Dict[str, Any]] = {}
        self.load()

    def load(self) -> bool:
        path = str(self.path or "").strip()
        if not path or not os.path.exists(path):
            return False
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            logger.warning("[SelectorBlacklist] load: failed to read %s", path, exc_info=True)
            return False

        markets = raw.get("markets") if isinstance(raw, dict) else None
        if not isinstance(markets, dict):
            return False

        out: Dict[str, Dict[str, Any]] = {}
        for market, row in markets.items():
            mk = _norm_market(market)
            if mk and isinstance(row, dict):
                out[mk] = dict(row)

        with self._lock:
            self._markets = out
        return True

    def save(self) -> bool:
        from app.core.io_utils import safe_write_json
        path = str(self.path or "").strip()
        if not path:
            return False
        try:
            with self._lock:
                data = {
                    "ts": _now_ts(),
                    "markets": dict(self._markets or {}),
                }
            safe_write_json(path, data)
            return True
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            logger.warning("[SelectorBlacklist] save: failed to write %s", path, exc_info=True)
            return False

    def prune(self, *, now_ts: Optional[float] = None) -> int:
        now_ts = float(now_ts or _now_ts())
        removed = 0
        with self._lock:
            kept: Dict[str, Dict[str, Any]] = {}
            for market, row in (self._markets or {}).items():
                try:
                    until_ts = float((row or {}).get("until_ts") or 0.0)
                except (KeyError, AttributeError, TypeError, ValueError):
                    logger.warning("[SelectorBlacklist] prune: until_ts parse failed for %s", market, exc_info=True)
                    until_ts = 0.0
                if until_ts > now_ts:
                    kept[market] = row
                else:
                    removed += 1
            self._markets = kept
        if removed:
            self.save()
        return removed

    def get_active_markets(self, *, now_ts: Optional[float] = None) -> Set[str]:
        now_ts = float(now_ts or _now_ts())
        out: Set[str] = set()
        with self._lock:
            for market, row in (self._markets or {}).items():
                try:
                    until_ts = float((row or {}).get("until_ts") or 0.0)
                except (KeyError, AttributeError, TypeError, ValueError):
                    logger.warning("[SelectorBlacklist] get_active: until_ts parse failed for %s", market, exc_info=True)
                    until_ts = 0.0
                if until_ts > now_ts:
                    out.add(market)
        return out

    def snapshot(self, *, now_ts: Optional[float] = None) -> Dict[str, Any]:
        now_ts = float(now_ts or _now_ts())
        active = self.get_active_markets(now_ts=now_ts)
        with self._lock:
            return {
                "path": str(self.path or ""),
                "active_count": len(active),
                "markets": dict(self._markets or {}),
            }

    def apply_signals(
        self,
        signals: Sequence[SelectorBlacklistSignal],
        *,
        now_ts: Optional[float] = None,
    ) -> Dict[str, Any]:
        now_ts = float(now_ts or _now_ts())
        applied = 0
        extended = 0
        by_reason: Counter[str] = Counter()

        with self._lock:
            for sig in signals or []:
                market = _norm_market(getattr(sig, "market", ""))
                reason = str(getattr(sig, "reason", "") or "").strip().lower()
                ttl_sec = max(0.0, float(getattr(sig, "ttl_sec", 0.0) or 0.0))
                source = str(getattr(sig, "source", "") or "").strip() or "selector"
                meta = dict(getattr(sig, "meta", {}) or {})
                if not market or not reason or ttl_sec <= 0.0:
                    continue

                until_ts = now_ts + ttl_sec
                cur = dict((self._markets or {}).get(market) or {})
                prev_until = 0.0
                try:
                    prev_until = float(cur.get("until_ts") or 0.0)
                except (KeyError, AttributeError, TypeError, ValueError):
                    logger.warning("[SelectorBlacklist] apply_signals: prev_until parse failed for %s", market, exc_info=True)
                    prev_until = 0.0

                reasons = dict(cur.get("reasons") or {})
                reasons[reason] = int(reasons.get(reason, 0) or 0) + 1

                cur["market"] = market
                cur["created_ts"] = float(cur.get("created_ts") or now_ts)
                cur["updated_ts"] = now_ts
                cur["until_ts"] = max(prev_until, until_ts)
                cur["hit_count"] = int(cur.get("hit_count") or 0) + 1
                cur["last_reason"] = reason
                cur["last_source"] = source
                cur["last_ttl_sec"] = float(ttl_sec)
                cur["reasons"] = reasons
                if meta:
                    cur["last_meta"] = meta

                if prev_until > 0.0 and cur["until_ts"] > prev_until:
                    extended += 1
                applied += 1
                by_reason[reason] += 1
                self._markets[market] = cur

        if applied:
            self.save()
        return {
            "applied": int(applied),
            "extended": int(extended),
            "by_reason": dict(by_reason),
        }

selector_blacklist_store = SelectorBlacklistStore()

def selector_blacklist_enabled() -> bool:
    return _bool_env("OMA_SELECTOR_BLACKLIST_ENABLED", True)
