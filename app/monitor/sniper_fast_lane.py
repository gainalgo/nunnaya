# ============================================================
# File: app/monitor/sniper_fast_lane.py
# Autocoin OS v3-H — Sniper Fast Lane
# ============================================================
"""Proactive SNIPER entry: bypasses Reserved Queue for immediate
buy when a coin in the Warm Pool drops sharply.

Flow:
  1. Maintain Warm Pool — top 30 markets by SNIPER affinity
     (refreshed every ~10 min from reserved_selector cache).
  2. Every 30 s (via tick_loop), sample each Warm Pool price.
  3. On drop >= threshold (-1.5 %):
     a. Run compute_scope_score() (5-min candle, 6-stage gate).
     b. If rank_score >= 55 → Direct ACTIVE promotion
        (oma_set_market + pre-seeded price history).
  4. Safety: max 3 per day, BTC Guard respected, budget checked.

Enable:  SNIPER_FAST_LANE_ENABLED=true  (default: false)
"""
from __future__ import annotations
from app.core.currency import Q

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger("sniper_fast_lane")

# ── config ────────────────────────────────────────────────────
def _env_bool(key: str, default: bool = False) -> bool:
    v = str(os.getenv(key, str(default))).strip().lower()
    return v in ("1", "true", "yes", "on")

def _env_int(key: str, default: int = 0) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except (TypeError, ValueError):
        logger.warning("[FastLane] env int parse failed: %s, using default %s", key, default, exc_info=True)
        return default

def _env_float(key: str, default: float = 0.0) -> float:
    try:
        return float(os.getenv(key, str(default)))
    except (TypeError, ValueError):
        logger.warning("[FastLane] env float parse failed: %s, using default %s", key, default, exc_info=True)
        return default

ENABLED          = _env_bool("SNIPER_FAST_LANE_ENABLED", False)
DROP_THRESHOLD   = _env_float("SNIPER_FAST_LANE_DROP_PCT", -1.5)   # %
# [FIX 2026-03-23] default raised 55→65 (unified with SNIPER autopilot confidence 65%)
SCORE_THRESHOLD  = _env_float("SNIPER_FAST_LANE_SCORE_MIN", 65.0)
WARM_POOL_SIZE   = _env_int("SNIPER_FAST_LANE_POOL_SIZE", 30)
POOL_REFRESH_SEC = _env_float("SNIPER_FAST_LANE_POOL_REFRESH_SEC", 600.0)
CHECK_INTERVAL   = _env_float("SNIPER_FAST_LANE_CHECK_SEC", 30.0)
MAX_DAILY         = _env_int("SNIPER_FAST_LANE_MAX_DAILY", 3)
MIN_BUDGET_USDT   = _env_float("SNIPER_FAST_LANE_MIN_BUDGET", 10_000.0)
COOLDOWN_SEC     = _env_float("SNIPER_FAST_LANE_COOLDOWN_SEC", 300.0)

# ── data ──────────────────────────────────────────────────────
@dataclass
class WarmEntry:
    """Candidate kept in warm pool with its last-known price."""
    market: str
    last_price: float = 0.0
    last_ts: float = 0.0
    baseline_price: float = 0.0     # price at pool-refresh time
    baseline_ts: float = 0.0

@dataclass
class FastLaneStats:
    """Daily counters — reset at midnight."""
    date_str: str = ""
    activations: int = 0
    drops_detected: int = 0
    scope_checks: int = 0
    scope_pass: int = 0

class SniperFastLane:
    """Singleton-style object, owned by HyperSystem."""

    def __init__(self):
        self.enabled: bool = ENABLED
        self._pool: Dict[str, WarmEntry] = {}         # market → entry
        self._pool_refresh_ts: float = 0.0
        self._last_check_ts: float = 0.0
        self._stats = FastLaneStats()
        self._cooldowns: Dict[str, float] = {}        # market → expiry_ts
        self._activated_today: Set[str] = set()
        self._day_str: str = ""
        logger.info(
            f"[FastLane] init  enabled={self.enabled}  "
            f"drop={DROP_THRESHOLD}%  score>={SCORE_THRESHOLD}  "
            f"pool={WARM_POOL_SIZE}  max_daily={MAX_DAILY}"
        )

    # ────────────────────────────────────────────────────────
    #  Warm Pool management
    # ────────────────────────────────────────────────────────
    def refresh_pool(self, system: Any) -> None:
        """Rebuild warm pool from the latest ticker + basic filters.

        Called periodically (every POOL_REFRESH_SEC).
        Uses Bybit ticker API (already cached by system if possible).
        """
        now = time.time()
        if (now - self._pool_refresh_ts) < POOL_REFRESH_SEC and self._pool:
            return

        try:
            import requests as _req

            # get all USDT tickers
            # [FIX N6] avoid URL length overflow when passing 200+ markets in one call — chunk by 100
            all_markets = self._get_quote_markets()
            tickers: list = []
            for _chunk_start in range(0, len(all_markets), 100):
                _chunk = all_markets[_chunk_start:_chunk_start + 100]
                from app.core.constants import (
                    BYBIT_MARKET_TICKERS,
                    bybit_v5_rest_category,
                    parse_bybit_list,
                    normalize_bybit_ticker,
                )
                _r = _req.get(
                    BYBIT_MARKET_TICKERS,
                    params={"category": bybit_v5_rest_category()},
                    timeout=10,
                )
                _r.raise_for_status()
                _chunk_set = set(c.upper() for c in _chunk)
                _all = parse_bybit_list(_r.json())
                tickers.extend([normalize_bybit_ticker(t) for t in _all if isinstance(t, dict) and t.get("symbol","") in _chunk_set])
                break  # Bybit returns all tickers at once
        except (KeyError, AttributeError, TypeError, ValueError) as exc:
            logger.warning("[sniper_fast_lane] %s: %s", "[FastLane] pool refresh failed", exc, exc_info=True)
            return

        # rank by 24h volume * inverse price (SNIPER prefers cheap volatile coins)
        scored: List[tuple] = []
        already_active = self._get_active_markets(system)

        for tk in tickers:
            m = str(tk.get("market") or "")
            if not m.endswith("USDT"):
                continue
            if m in already_active:
                continue
            price = float(tk.get("trade_price") or 0)
            vol24 = float(tk.get("acc_trade_price_24h") or 0)
            if price <= 0 or vol24 < 500_000:    # min 500k USDT 24h turnover
                continue
            # basic affinity: higher volume + moderate price
            scored.append((m, vol24, price))

        # sort by volume descending, take top N
        scored.sort(key=lambda x: x[1], reverse=True)
        new_pool: Dict[str, WarmEntry] = {}
        for m, vol, price in scored[:WARM_POOL_SIZE]:
            old = self._pool.get(m)
            if old and old.baseline_price > 0:
                # keep baseline if refreshed recently
                new_pool[m] = WarmEntry(
                    market=m,
                    last_price=old.last_price if old.last_price > 0 else price,
                    last_ts=old.last_ts or now,
                    baseline_price=price,
                    baseline_ts=now,
                )
            else:
                new_pool[m] = WarmEntry(
                    market=m,
                    last_price=price,
                    last_ts=now,
                    baseline_price=price,
                    baseline_ts=now,
                )

        self._pool = new_pool
        self._pool_refresh_ts = now
        logger.info(f"[FastLane] pool refreshed: {len(self._pool)} markets")

    def update_price(self, market: str, price: float) -> None:
        """Called from tick_loop or price_feed for each known market."""
        entry = self._pool.get(market)
        if entry and price > 0:
            entry.last_price = price
            entry.last_ts = time.time()

    # ────────────────────────────────────────────────────────
    #  Drop detection + activation
    # ────────────────────────────────────────────────────────
    def check(self, system: Any) -> List[str]:
        """Main entry: detect drops, score, activate.

        Returns list of markets that were fast-lane activated.
        Called from tick_loop every CHECK_INTERVAL seconds.
        """
        if not self.enabled:
            return []

        now = time.time()
        if (now - self._last_check_ts) < CHECK_INTERVAL:
            return []
        self._last_check_ts = now

        # day rollover
        import datetime
        today = datetime.date.today().isoformat()
        if today != self._day_str:
            self._day_str = today
            self._stats = FastLaneStats(date_str=today)
            self._activated_today.clear()
            self._cooldowns.clear()  # [FIX N5] clear expired cooldowns (prevent unbounded growth)

        # budget check
        if self._stats.activations >= MAX_DAILY:
            return []

        # BTC Guard check
        if getattr(system, "btc_guard_mode", False):
            return []

        # refresh pool if needed
        self.refresh_pool(system)

        activated: List[str] = []
        already_active = self._get_active_markets(system)

        for market, entry in list(self._pool.items()):
            if market in already_active:
                continue
            if market in self._activated_today:
                continue
            if self._cooldowns.get(market, 0) > now:
                continue
            if entry.baseline_price <= 0 or entry.last_price <= 0:
                continue

            # calculate drop from baseline
            drop_pct = ((entry.last_price - entry.baseline_price) / entry.baseline_price) * 100.0
            if drop_pct > DROP_THRESHOLD:
                continue  # not enough drop (threshold is negative)

            self._stats.drops_detected += 1
            logger.info(
                f"[FastLane] DROP detected: {market} "
                f"{entry.baseline_price:.1f} → {entry.last_price:.1f} "
                f"({drop_pct:+.2f}%)"
            )

            # scope score check
            result = self._run_scope_check(market)
            if result is None:
                self._cooldowns[market] = now + COOLDOWN_SEC
                continue

            rank_score = float(result.get("rank_score") or 0)
            if rank_score < SCORE_THRESHOLD:
                logger.info(
                    f"[FastLane] {market} scope score {rank_score:.1f} < {SCORE_THRESHOLD} — skip"
                )
                self._cooldowns[market] = now + COOLDOWN_SEC
                continue

            self._stats.scope_pass += 1

            # budget check
            budget = self._calc_budget(system)
            if budget < MIN_BUDGET_USDT:
                logger.info(f"[FastLane] insufficient budget ({budget:.0f} < {MIN_BUDGET_USDT:.0f})")
                continue

            # [FIX L2] check emergency_stop / global entry block (previously missing)
            if getattr(system, "emergency_stop", False):
                logger.info("[FastLane] emergency_stop active — aborting scan")
                break
            global_block_until = float(getattr(system, "global_entry_block_until_ts", 0.0) or 0.0)
            if global_block_until > now:
                logger.info(f"[FastLane] global_entry_block active for {global_block_until - now:.0f}s — skip {market}")
                continue

            # DO IT: activate
            ok = self._activate(system, market, result, budget)
            if ok:
                activated.append(market)
                self._activated_today.add(market)
                self._stats.activations += 1
                self._cooldowns[market] = now + COOLDOWN_SEC
                if self._stats.activations >= MAX_DAILY:
                    break

        return activated

    def _run_scope_check(self, market: str) -> Optional[Dict[str, Any]]:
        """Run compute_scope_score() on the market."""
        self._stats.scope_checks += 1
        try:
            from app.api.strategy_router import compute_scope_score
            return compute_scope_score(market)
        except (ImportError, AttributeError, TypeError) as e:
            logger.warning(f"[FastLane] scope check failed for {market}: {e}")
            return None

    def _calc_budget(self, system: Any) -> float:
        """Calculate available budget for a fast-lane entry."""
        try:
            total = float(getattr(system, "total_capital_usdt", 0) or 0)
            allocated = 0.0
            for ctx in getattr(system, "coordinator", object).__dict__.get("_contexts", {}).values():
                allocated += float(getattr(ctx, "allocated_capital", 0) or 0)
            available = total - allocated
            # use min(available * 10%, per-slot budget)
            active_count = len(self._get_active_markets(system))
            per_slot = total / max(1, active_count + 5)  # conservative division
            budget = min(available * 0.10, per_slot)
            return max(0.0, budget)
        except (KeyError, AttributeError, TypeError, ValueError) as e:
            logger.warning(f"[FastLane] budget calc error: {e}")
            return 0.0

    def _activate(
        self,
        system: Any,
        market: str,
        scope_result: Dict[str, Any],
        budget: float,
    ) -> bool:
        """Promote market to ACTIVE via oma_set_market, pre-seed price history."""
        try:
            # build params from scope result
            params = scope_result.get("optimal_params") or {}
            tp_pct = float(params.get("tp_pct") or 3.0)
            sl_pct = float(params.get("sl_pct") or 5.0)
            entry_threshold = float(params.get("entry_threshold") or 1.5)

            logger.info(
                f"[FastLane] ACTIVATING {market}  "
                f"score={scope_result.get('rank_score', 0):.1f}  "
                f"budget={budget:.0f}  tp={tp_pct:.1f}%  sl={sl_pct:.1f}%"
            )

            # 1. oma_set_market → ACTIVE with SNIPER strategy
            from app.core.constants import MarketState
            system.oma_set_market(
                market,
                MarketState.ACTIVE,
                reason=[f"FastLane: score={scope_result.get('rank_score',0):.1f}"],
                budget_usdt=budget,
            )

            # 2. Set strategy to SNIPER + scope params
            ctx = None
            try:
                ctx = system.coordinator.get_context(market)
            except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
                logger.warning("[sniper_fast_lane] %s: %s", '2. Set strategy to SNIPER + scope params', exc, exc_info=True)

            if ctx is None:
                # [FIX #5] without ctx the strategy can't be set → activation fails
                logger.warning(f"[FastLane] ctx is None for {market}, activation aborted")
                return False

            ctx.strategy = "SNIPER"
            # [FIX 2026-03-05] controls["strategy"] is a dict → no object attribute mutation
            _ctrl_dict = getattr(ctx, "controls", None)
            if isinstance(_ctrl_dict, dict):
                _ctrl_dict.setdefault("strategy", {})
                _ctrl_dict["strategy"].update({
                    "enabled": True,
                    "mode": "SNIPER",
                    "params": {
                        "tp_pct": tp_pct,
                        "sl_pct": sl_pct,
                        "entry_threshold": entry_threshold,
                        "source": "fast_lane",
                        "scope_score": float(scope_result.get("rank_score") or 0),
                        "fast_lane_ts": time.time(),
                    },
                })
            # 3. Pre-seed price history from scope result
            self._pre_seed_prices(ctx, market)

            # 4. Notify
            try:
                from app.notify.telegram import send_telegram
                send_telegram(
                    f"🎯 [FastLane] {market} promoted to ACTIVE\n"
                    f"Score: {scope_result.get('rank_score',0):.1f}\n"
                    f"Price: {scope_result.get('price',0):,.0f}\n"
                    f"Budget: {budget:,.2f} USDT\n"
                    f"TP: {tp_pct:.1f}% / SL: {sl_pct:.1f}%"
                )
            except (KeyError, AttributeError, TypeError, ValueError) as exc:
                logger.warning("[sniper_fast_lane] %s: %s", '4. Notify', exc, exc_info=True)

            return True

        except (OSError, KeyError, AttributeError, TypeError, ValueError, OverflowError) as e:
            logger.error(f"[FastLane] activation failed for {market}: {e}", exc_info=True)
            return False

    def _pre_seed_prices(self, ctx: Any, market: str) -> None:
        """Pre-seed price_history from 5-min candles for immediate warmup."""
        try:
            from app.core.multi_timeframe_ai import fetch_candles
            candles = fetch_candles(market, unit=5, count=60)
            if not candles:
                return
            prices = [float(c.get("trade_price") or c.get("close") or 0) for c in candles if float(c.get("trade_price") or c.get("close") or 0) > 0]
            if hasattr(ctx, "pre_seed_prices"):
                ctx.pre_seed_prices(prices)
            else:
                # fallback: manual seed
                for p in prices:
                    ctx.record_price(p)
                ctx.force_ready()
            logger.info(f"[FastLane] pre-seeded {len(prices)} prices for {market}")
        except (KeyError, AttributeError, TypeError, ValueError) as e:
            logger.warning(f"[FastLane] pre-seed failed for {market}: {e}")

    # ────────────────────────────────────────────────────────
    #  Helpers
    # ────────────────────────────────────────────────────────
    def _get_quote_markets(self) -> List[str]:
        """Get all USDT spot market codes from Bybit."""
        try:
            from app.core.rate_limiter import bybit_get
            from app.core.constants import BYBIT_MARKET_INSTRUMENTS, bybit_v5_rest_category, parse_bybit_list
            resp = bybit_get(BYBIT_MARKET_INSTRUMENTS, params={"category": bybit_v5_rest_category()}, timeout=10)
            resp.raise_for_status()
            return [
                str(m.get("symbol", ""))
                for m in parse_bybit_list(resp.json())
                if isinstance(m, dict) and str(m.get("quoteCoin", "")) == Q.symbol
            ]
        except (KeyError, AttributeError, TypeError, ValueError):
            logger.warning("[FastLane] known_markets lookup failed", exc_info=True)
            return []

    def _get_active_markets(self, system: Any) -> Set[str]:
        """Get currently ACTIVE + RECOVERY + WATCH markets from OMA."""
        try:
            reg = getattr(system, "oma_registry", None)
            if reg is None:
                return set()
            active = set(reg.list_active() or [])
            active.update(reg.list_recovery() or [])
            active.update(reg.list_watch() or [])
            if hasattr(reg, "list_prewarm"):
                active.update(reg.list_prewarm() or [])
            return active
        except (KeyError, AttributeError, TypeError):
            logger.warning("[FastLane] active_markets lookup failed", exc_info=True)
            return set()

    def get_stats(self) -> Dict[str, Any]:
        """Return stats for API / dashboard."""
        return {
            "enabled": self.enabled,
            "pool_size": len(self._pool),
            "pool_markets": sorted(self._pool.keys()),
            "date": self._stats.date_str,
            "activations_today": self._stats.activations,
            "drops_detected": self._stats.drops_detected,
            "scope_checks": self._stats.scope_checks,
            "scope_pass": self._stats.scope_pass,
            "max_daily": MAX_DAILY,
            "drop_threshold": DROP_THRESHOLD,
            "score_threshold": SCORE_THRESHOLD,
        }
