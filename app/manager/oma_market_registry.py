# ============================================================
# File: app/manager/oma_market_registry.py
# Autocoin OS v3-H — OMA Market Registry (PERSISTENT + RECOVERY)
# ============================================================

from __future__ import annotations

import json
import logging
import os
import time
import threading
from enum import Enum
from typing import Dict, Any, List, Optional

from app.core.currency import Q

logger = logging.getLogger(__name__)

def _f(x: Any, default: float = 0.0) -> float:
    try:
        if x is None:
            return default
        return float(x)
    except (TypeError, ValueError):
        logger.warning(f"_f: failed to convert {x!r} to float, using default={default}", exc_info=True)
        return default


class MarketState(str, Enum):
    ACTIVE = "ACTIVE"
    WATCH = "WATCH"
    RECOVERY = "RECOVERY"   # orphan/exit-only managed target
    DISABLED = "DISABLED"

class OMAMarketRegistry:
    """Single ledger of OMA market administrative state.

    Enhancements:
    - Added RECOVERY state (entry blocked + reclaim management)
    - Saved/restored to runtime/oma_state.json (persists across server resets)
    """

    def __init__(self, *, state_path: Optional[str] = None):
        self._markets: Dict[str, Dict[str, Any]] = {}
        self.unlock_history: List[Dict[str, Any]] = []
        self._lock = threading.RLock()

        self.state_path = state_path or os.getenv("OMA_STATE_PATH", "runtime/oma_state.json")
        self._active_since = {}  # market -> timestamp

        # Prewarm subscription set (ephemeral, not persisted)
        # - Used to warm candidate markets before promotion (rolling replacement)
        self._prewarm: Dict[str, float] = {}  # market -> added_ts

    # --------------------------------------------------------
    # Persistence
    # --------------------------------------------------------
    def load(self) -> bool:
        with self._lock:
            path = self.state_path
            if not path or not os.path.exists(path):
                return False

            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except (OSError, json.JSONDecodeError) as e:
                logger.warning("OMAMarketRegistry.load: failed to read %s: %s", path, e)
                return False

            markets = data.get("markets")
            if isinstance(markets, dict):
                restored: Dict[str, Dict[str, Any]] = {}
                restored_active_since: Dict[str, float] = {}
                for m, v in markets.items():
                    if not isinstance(v, dict):
                        continue
                    st_raw = v.get("state")
                    try:
                        st = MarketState(str(st_raw))
                    except ValueError:
                        logger.warning(f"OMAMarketRegistry.load: invalid state {st_raw!r} for {m}, defaulting DISABLED", exc_info=True)
                        st = MarketState.DISABLED

                    # PATCH: budget_usdt restore (manual budget per market)
                    budget = v.get("budget_usdt")
                    try:
                        budget_f = float(budget) if budget is not None else None
                    except (TypeError, ValueError):
                        logger.warning(f"OMAMarketRegistry.load: invalid budget {budget!r} for {m}", exc_info=True)
                        budget_f = None

                    active_since_raw = v.get("active_since_ts")
                    try:
                        active_since_f = float(active_since_raw) if active_since_raw is not None else None
                    except (TypeError, ValueError):
                        logger.warning(f"OMAMarketRegistry.load: invalid active_since_ts {active_since_raw!r} for {m}", exc_info=True)
                        active_since_f = None
                    if active_since_f is not None and active_since_f <= 0:
                        active_since_f = None

                    restored[str(m)] = {
                        "state": st,
                        "reason": list(v.get("reason") or []),
                        "budget_usdt": budget_f,
                        "active_since_ts": active_since_f,
                    }
                    if st == MarketState.ACTIVE and active_since_f is not None:
                        restored_active_since[str(m)] = active_since_f
                self._markets = restored
                self._active_since = restored_active_since

            uh = data.get("unlock_history")
            if isinstance(uh, list):
                self.unlock_history = list(uh)

            return True

    def save(self) -> bool:
        from app.core.io_utils import safe_write_json
        path = self.state_path
        if not path:
            return False

        try:
            with self._lock:
                data = {
                    "ts": time.time(),
                    "markets": {
                        m: {
                            "state": (v.get("state").value if isinstance(v.get("state"), MarketState) else str(v.get("state"))),
                            "reason": list(v.get("reason") or []),
                            "budget_usdt": v.get("budget_usdt"),
                            "active_since_ts": v.get("active_since_ts"),
                        }
                        for m, v in self._markets.items()
                    },
                    "unlock_history": list(self.unlock_history),
                }
                safe_write_json(path, data)
                return True
        except OSError as e:
            logger.warning("OMAMarketRegistry.save: failed to save %s: %s", path, e)
            return False
        
    def set_market(self, market: str, state: MarketState, reason=None):
        with self._lock:
            now = time.time()

            if state == MarketState.ACTIVE:
                self._active_since[market] = now

    def _should_demote_to_watch(self, market: str) -> bool:
        # Minimum ACTIVE hold time (e.g., 10 minutes)
        MIN_ACTIVE_SEC = 600

        since = self._active_since.get(market)
        if since and (time.time() - since) < MIN_ACTIVE_SEC:
            return False

        # Keep existing decision logic
        return True

    # --------------------------------------------------------
    # WRITE (administration)
    # --------------------------------------------------------
    def set_state(
        self,
        market: str,
        state: MarketState,
        reason: List[str] | None = None,
        *,
        budget_usdt: float | None = None,
        persist: bool = True,
    ):
        with self._lock:
            # PATCH: manual budget hard-lock storage (budget_usdt)
            # - budget_usdt is preserved unless explicitly provided
            # - budget_usdt <= 0 clears the manual budget
            existing = self._markets.get(str(market), {})
            prev_budget = existing.get("budget_usdt")
            prev_active_since = existing.get("active_since_ts")

            prev_state_raw = existing.get("state")
            if prev_state_raw is None:
                prev_state = None
            elif isinstance(prev_state_raw, MarketState):
                prev_state = prev_state_raw
            else:
                try:
                    prev_state = MarketState(str(prev_state_raw))
                except ValueError:
                    logger.warning("set_state: invalid prev_state=%r for %s", prev_state_raw, market, exc_info=True)
                    prev_state = None

            try:
                prev_active_since_f = float(prev_active_since) if prev_active_since is not None else None
            except (TypeError, ValueError):
                logger.warning("set_state: invalid prev_active_since=%r for %s", prev_active_since, market, exc_info=True)
                prev_active_since_f = None

            # ★ State transition validation (Upbit sync 2026-04-05)
            new_state = MarketState(state)
            if prev_state == MarketState.DISABLED and new_state == MarketState.RECOVERY:
                logger.warning("[OMA] BLOCKED transition DISABLED→RECOVERY for %s (must go WATCH first)", market)
                return
            if prev_state == MarketState.RECOVERY and new_state == MarketState.ACTIVE:
                logger.warning("[OMA] WARN: RECOVERY→ACTIVE for %s (usually goes via WATCH)", market)
            if prev_active_since_f is not None and prev_active_since_f <= 0:
                prev_active_since_f = None

            new_budget = prev_budget
            if budget_usdt is not None:
                try:
                    b = float(budget_usdt)
                except (TypeError, ValueError):
                    logger.warning(f"set_state: invalid budget_usdt={budget_usdt!r} for {market}", exc_info=True)
                    b = None

                if b is None or b <= 0:
                    new_budget = None
                else:
                    new_budget = b

            self._markets[str(market)] = {
                "state": MarketState(state),
                "reason": reason or [],
                "budget_usdt": new_budget,
                "active_since_ts": prev_active_since_f,
            }

            # ACTIVE entry timestamp:
            # - Newly recorded only on non-ACTIVE -> ACTIVE transition
            # - If already ACTIVE, keep existing ts (so budget/reason updates don't reset age)
            try:
                if MarketState(state) == MarketState.ACTIVE:
                    if prev_state == MarketState.ACTIVE and prev_active_since_f is not None:
                        active_since_ts = float(prev_active_since_f)
                    else:
                        active_since_ts = time.time()
                    self._markets[str(market)]["active_since_ts"] = active_since_ts
                    self._active_since[str(market)] = active_since_ts
            except (ValueError, KeyError) as e:
                logger.warning("set_state: failed to record active_since_ts for %s: %s", market, e, exc_info=True)

            if MarketState(state) != MarketState.ACTIVE:
                try:
                    self._markets[str(market)].pop("active_since_ts", None)
                    self._active_since.pop(str(market), None)
                except KeyError as exc:
                    logger.warning("[oma_market_registry] %s: %s", '- if already ACTIVE, keep existing ts (so budget/reason updates do not reset age)', exc, exc_info=True)

            # 2026-03-10: Record WATCH entry timestamp (for auto-DISABLED timeout)
            try:
                if MarketState(state) == MarketState.WATCH:
                    prev_watch_ts = existing.get("watch_since_ts")
                    if prev_state == MarketState.WATCH and prev_watch_ts:
                        self._markets[str(market)]["watch_since_ts"] = float(prev_watch_ts)
                    else:
                        self._markets[str(market)]["watch_since_ts"] = time.time()
                else:
                    self._markets[str(market)].pop("watch_since_ts", None)
            except (ValueError, KeyError) as exc:
                logger.warning("[oma_market_registry] %s: %s", '2026-03-10: Record WATCH entry timestamp (for auto-DISABLED timeout)', exc, exc_info=True)

            if persist:
                self.save()

    def has_market(self, market: str) -> bool:
        with self._lock:
            """Return True if the market exists in the registry (i.e., explicitly tracked)."""
            return str(market) in self._markets

    def get_state(self, market: str) -> MarketState:
        with self._lock:
            v = self._markets.get(str(market))
            if not v:
                return MarketState.DISABLED
            st = v.get("state")
            if isinstance(st, MarketState):
                return st
            try:
                return MarketState(str(st))
            except ValueError:
                logger.warning(f"get_state: invalid state {st!r} for {market}, returning DISABLED", exc_info=True)
                return MarketState.DISABLED

    def get_budget_usdt(self, market: str) -> float | None:
        with self._lock:
            """Return the manual budget (USDT) for a market, if configured."""
            v = self._markets.get(str(market))
            if not v:
                return None
            b = v.get("budget_usdt")
            try:
                return float(b) if b is not None else None
            except (TypeError, ValueError):
                logger.warning(f"get_budget_usdt: invalid budget {b!r} for {market}", exc_info=True)
                return None

    def get_reason(self, market: str) -> List[str]:
        with self._lock:
            v = self._markets.get(str(market))
            if not v:
                return []
            return list(v.get("reason") or [])

    def get_active_since_ts(self, market: str) -> float | None:
        with self._lock:
            """Return ACTIVE entry timestamp for a market, if available."""
            v = self._markets.get(str(market))
            if not v:
                return None
            ts = v.get('active_since_ts')
            try:
                return float(ts) if ts is not None else None
            except (TypeError, ValueError):
                logger.warning(f"get_active_since_ts: invalid ts {ts!r} for {market}", exc_info=True)
                return None

    def list_all(self) -> List[str]:
        with self._lock:
            """Return all markets currently tracked in the registry."""
            return list(self._markets.keys())

    # --------------------------------------------------------
    # READ
    # --------------------------------------------------------
    def list_active(self) -> List[str]:
        with self._lock:
            return [m for m, v in self._markets.items() if v.get("state") == MarketState.ACTIVE]

    def list_watch(self) -> List[str]:
        with self._lock:
            return [m for m, v in self._markets.items() if v.get("state") == MarketState.WATCH]

    def list_recovery(self) -> List[str]:
        with self._lock:
            return [m for m, v in self._markets.items() if v.get("state") == MarketState.RECOVERY]

    # --------------------------------------------------------
    # PREWARM (ephemeral subscription set)
    # --------------------------------------------------------
    def is_prewarm(self, market: str) -> bool:
        with self._lock:
            m = str(market or "").strip().upper()
            if not m:
                return False
            return m in self._prewarm

    def list_prewarm(self) -> List[str]:
        with self._lock:
            try:
                return sorted(list(self._prewarm.keys()))
            except (TypeError, AttributeError) as e:
                logger.warning("list_prewarm: failed to list prewarm: %s", e, exc_info=True)
                return []

    def set_prewarm(self, market: str, enabled: bool = True) -> bool:
        with self._lock:
            """Add/remove a market from the PREWARM subscription set.

            Returns True if the set changed.
            """
            m = str(market or "").strip().upper()
            if not m:
                return False

            if enabled:
                if m in self._prewarm:
                    return False
                self._prewarm[m] = time.time()
                return True

            # disable
            return (self._prewarm.pop(m, None) is not None)

    def replace_prewarm(self, markets: List[str]) -> Dict[str, Any]:
        with self._lock:
            """Replace PREWARM set with the given list.

            Returns:
              {"added": [...], "removed": [...], "size": int}
            """
            now = time.time()
            new_set = set()
            for x in markets or []:
                m = str(x or "").strip().upper()
                if m:
                    new_set.add(m)

            old_set = set(self._prewarm.keys())
            added = sorted(list(new_set - old_set))
            removed = sorted(list(old_set - new_set))

            for m in added:
                self._prewarm[m] = now
            for m in removed:
                self._prewarm.pop(m, None)

            return {"added": added, "removed": removed, "size": int(len(new_set))}

    def snapshot(self) -> Dict[str, Any]:
        def _extract_strategy(reasons: list) -> str:
            """Extract strategy from the reason list.
            1) strategy:XXX tag takes priority
            2) Otherwise, search the reason strings for strategy keywords
            """
            STRATEGY_KEYWORDS = ["PINGPONG", "AUTOLOOP", "LADDER", "LIGHTNING", "GAZUA", "CONTRARIAN", "SNIPER"]
            
            # 1) strategy:XXX format
            for r in (reasons or []):
                if isinstance(r, str) and r.upper().startswith("STRATEGY:"):
                    return r.split(":", 1)[1].strip().upper()
            
            # 2) Find strategy keyword in reason (e.g., "pingpong_budget_restore")
            for r in (reasons or []):
                if isinstance(r, str):
                    r_upper = r.upper()
                    for kw in STRATEGY_KEYWORDS:
                        if kw in r_upper:
                            return kw
            return ""
        
        with self._lock:
            return {
                "active": [
                    {
                        "market": m,
                        "reason": list(v.get("reason") or []),
                        "budget_usdt": v.get("budget_usdt"),
                        "strategy": _extract_strategy(v.get("reason")),
                    }
                    for m, v in self._markets.items()
                    if v.get("state") == MarketState.ACTIVE
                ],
                "watch": [
                    {
                        "market": m,
                        "reason": list(v.get("reason") or []),
                        "budget_usdt": v.get("budget_usdt"),
                        "strategy": _extract_strategy(v.get("reason")),
                    }
                    for m, v in self._markets.items()
                    if v.get("state") == MarketState.WATCH
                ],
                "recovery": [
                    {
                        "market": m,
                        "reason": list(v.get("reason") or []),
                        "budget_usdt": v.get("budget_usdt"),
                        "strategy": _extract_strategy(v.get("reason")),
                    }
                    for m, v in self._markets.items()
                    if v.get("state") == MarketState.RECOVERY
                ],
                "unlock_history": list(self.unlock_history),
            }

# ------------------------------------------------------------
# Process-wide singleton instance
# ------------------------------------------------------------
oma_market_registry = OMAMarketRegistry()
