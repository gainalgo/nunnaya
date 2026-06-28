# ============================================================
# File: app/manager/sniper_position_store.py
# SNIPER position persistence manager
# ------------------------------------------------------------
# Keeps active SNIPER positions across server restarts.
# [2026-01-31] Multi-SNIPER support: allow multiple SNIPER instances per market
# ============================================================

import json
import os
import time
import threading
import uuid
from typing import Dict, Any, Optional, List
from pathlib import Path
import logging

logger = logging.getLogger(__name__)

SNIPER_POSITIONS_PATH = "runtime/sniper_positions.json"


def generate_sniper_id(market: str) -> str:
    """Generate a unique SNIPER ID.

    Format: {market}_sniper_{8-char uuid}
    Example: BTCUSDT_sniper_a1b2c3d4
    """
    return f"{market}_sniper_{uuid.uuid4().hex[:8]}"


def extract_market_from_id(sniper_id: str) -> Optional[str]:
    """Extract the market code from a SNIPER ID.

    Example: BTCUSDT_sniper_a1b2c3d4 -> BTCUSDT
    """
    if "_sniper_" in sniper_id:
        return sniper_id.split("_sniper_")[0]
    return sniper_id  # Legacy: sniper_id is the market itself


class SniperPositionStore:
    """SNIPER position store (multi-position support)."""

    def __init__(self):
        self._positions: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._load()

    def _load(self):
        """Load positions from file."""
        try:
            if os.path.exists(SNIPER_POSITIONS_PATH):
                with open(SNIPER_POSITIONS_PATH, "r", encoding="utf-8") as f:
                    self._positions = json.load(f)
                logger.info(f"[SNIPER] Loaded {len(self._positions)} positions from {SNIPER_POSITIONS_PATH}")
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as e:
            logger.warning("[SNIPER] Failed to load positions: %s", e)
            self._positions = {}

    def _save(self):
        """Save positions to file (atomic write - avoids corrupt JSON on crash)."""
        from app.core.io_utils import safe_write_json
        try:
            safe_write_json(SNIPER_POSITIONS_PATH, self._positions)
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as e:
            logger.warning("[SNIPER] Failed to save positions: %s", e)

    def save_position(self, sniper_id: str, data: Dict[str, Any]):
        """Save a position (ID-based).

        Args:
            sniper_id: unique SNIPER ID (e.g. BTCUSDT_sniper_a1b2c3d4) or market (legacy)
            data: position data
        """
        with self._lock:
            # Auto-add market info
            market = extract_market_from_id(sniper_id)
            # [FIX] Avoid duplicate legacy key (market itself) for the same market
            # When saving the new format (market_sniper_xxx), remove any leftover legacy key
            if "_sniper_" in sniper_id and market in self._positions and market != sniper_id:
                del self._positions[market]
                logger.info("[SNIPER] Cleaned legacy key %s (replaced by %s)", market, sniper_id)
            self._positions[sniper_id] = {
                **data,
                "market": market,
                "sniper_id": sniper_id,
                "ts": time.time(),
            }
            self._save()
            logger.info("[SNIPER] Saved position %s", sniper_id)

    def get_position(self, sniper_id: str) -> Optional[Dict[str, Any]]:
        """Get a position (ID-based)."""
        with self._lock:
            return self._positions.get(sniper_id)

    def get_positions_by_market(self, market: str) -> List[Dict[str, Any]]:
        """Get all SNIPER positions for a specific market.

        Args:
            market: market code (e.g. BTCUSDT)

        Returns:
            list of all SNIPER positions for that market
        """
        with self._lock:
            result = []
            for sniper_id, data in self._positions.items():
                # New format: extract market from sniper_id
                if sniper_id.startswith(f"{market}_sniper_"):
                    result.append({"sniper_id": sniper_id, **data})
                # Legacy format: sniper_id is the market itself
                elif sniper_id == market:
                    result.append({"sniper_id": sniper_id, **data})
            return result

    def remove_position(self, sniper_id: str):
        """Remove a position (ID-based)."""
        with self._lock:
            if sniper_id in self._positions:
                del self._positions[sniper_id]
                self._save()
                logger.info("[SNIPER] Removed position %s", sniper_id)
                return True
            return False

    def remove_positions_by_market(self, market: str) -> int:
        """Remove all SNIPER positions for a specific market.

        Returns:
            number of positions removed
        """
        with self._lock:
            to_remove = []
            for sniper_id in self._positions.keys():
                if sniper_id.startswith(f"{market}_sniper_") or sniper_id == market:
                    to_remove.append(sniper_id)
            
            for sniper_id in to_remove:
                del self._positions[sniper_id]
            
            if to_remove:
                self._save()
                logger.info(f"[SNIPER] Removed {len(to_remove)} positions for {market}")
            
            return len(to_remove)

    def get_all(self) -> Dict[str, Dict[str, Any]]:
        """Get all positions."""
        with self._lock:
            return dict(self._positions)

    def get_all_as_list(self) -> List[Dict[str, Any]]:
        """Get all positions as a list (including ID)."""
        with self._lock:
            return [
                {"sniper_id": k, **v}
                for k, v in self._positions.items()
            ]

    def restore_to_system(self, system) -> int:
        """Restore positions into the system. Returns the number restored.

        Multi-SNIPER support: since a market may have multiple positions,
        restore is grouped by market.
        SNIPERS(precision_scope) slots are capped so they do not exceed the target count.
        """
        restored = 0
        restored_markets = set()
        scope_target = max(0, int(getattr(system, "autopilot_scope_target_n",
                                          getattr(system, "reserved_sniper_n", 0)) or 0))
        scope_restored_count = 0
        
        with self._lock:
            # [FIX] Pre-clean duplicate legacy/new keys — avoid one market taking 2 slots
            _legacy_to_remove = []
            for sid in list(self._positions.keys()):
                market_of_sid = (self._positions[sid].get("market")
                                 or extract_market_from_id(sid))
                if "_sniper_" not in sid:  # legacy key (market name itself)
                    new_key_exists = any(
                        k != sid and "_sniper_" in k
                        and (self._positions[k].get("market") or extract_market_from_id(k)) == market_of_sid
                        for k in self._positions
                    )
                    if new_key_exists:
                        _legacy_to_remove.append(sid)
            for sid in _legacy_to_remove:
                del self._positions[sid]
                logger.info("[SNIPER] Removed duplicate legacy key %s during restore", sid)
            if _legacy_to_remove:
                self._save()

            for sniper_id, data in self._positions.items():
                is_scope = False  # [FIX #3] init outside try — avoid NameError on exception
                try:
                    # Extract market (new format or legacy)
                    market = data.get("market") or extract_market_from_id(sniper_id)

                    # Cap SNIPERS(scope) slots to target count: held positions are always restored
                    # [FIX #1] scope if profile=="SNIPERS" OR source=="precision_scope"
                    # the strategy_recommender path sets only profile (no source), so OR is needed
                    params = data.get("params", {}) or {}
                    is_scope = (str(params.get("profile") or "").strip().upper() == "SNIPERS"
                                or str(params.get("source") or "").strip().lower() == "precision_scope")
                    if is_scope and scope_target > 0:
                        has_qty = False
                        try:
                            _ctx = system.coordinator.contexts.get(market)
                            if _ctx:
                                _pos = getattr(_ctx, "position", None) or {}
                                has_qty = float(_pos.get("qty", 0) or 0) > 0
                        except (KeyError, AttributeError, TypeError, ValueError) as exc:
                            logger.warning("[SNIPER_STORE] strategy_recommender path OR check: %s", exc, exc_info=True)
                        if not has_qty and scope_restored_count >= scope_target:
                            logger.info("[SNIPER] Skip restore %s — scope target %d reached", market, scope_target)
                            continue

                    ctx = system.coordinator.get_context(market)
                    if not ctx:
                        ctx = system.coordinator.ensure_market(market)

                    # Restore strategy mode (once per market)
                    # Markets already set to the LADDER strategy are not overwritten with SNIPER
                    if market not in restored_markets:
                        existing_mode = ""
                        try:
                            ctrls = getattr(ctx, "controls", None) or {}
                            sc = ctrls.get("strategy") or {}
                            if isinstance(sc, dict) and bool(sc.get("enabled")):
                                existing_mode = str(sc.get("mode") or "").upper()
                        except (KeyError, AttributeError, TypeError, ValueError) as exc:
                            logger.warning("[SNIPER_STORE] error while checking LADDER strategy: %s", exc, exc_info=True)
                        if not existing_mode:
                            try:
                                sm = str(getattr(ctx, "strategy_mode", "") or "").strip().upper()
                                if sm:
                                    existing_mode = sm
                            except (KeyError, AttributeError, TypeError) as exc:
                                logger.warning("[SNIPER_STORE] strategy_mode lookup error: %s", exc, exc_info=True)
                        if existing_mode and existing_mode != "SNIPER":
                            logger.info("[SNIPER] Skip restore %s — existing strategy %s", market, existing_mode)
                            restored_markets.add(market)
                            continue
                        if not existing_mode:
                            try:
                                lm = getattr(system, "ladder_manager", None)
                                if lm:
                                    lcfg = lm.get_config(market)
                                    if lcfg.get("enabled"):
                                        existing_mode = "LADDER"
                            except (KeyError, AttributeError, TypeError) as exc:
                                logger.warning("[SNIPER_STORE] ladder_manager lookup error: %s", exc, exc_info=True)
                        if existing_mode == "LADDER":
                            logger.info("[SNIPER] Skip restore %s — already LADDER", market)
                            restored_markets.add(market)
                            continue

                        ctx.update_controls({
                            "strategy": {
                                "enabled": True,
                                "mode": "SNIPER",
                                "params": data.get("params", {}),
                            }
                        })
                        ctx.strategy_mode = "SNIPER"

                        # Restore state
                        from app.manager.oma_market_registry import MarketState
                        system.oma_set_market(
                            market=market,
                            state=MarketState.ACTIVE,
                            reason=["sniper_restore"],
                        )
                        restored_markets.add(market)

                    # Restore budget (idempotent): no cumulative summation
                    if data.get("budget_usdt") and hasattr(system, 'oma_registry'):
                        stored_budget = float(data.get("budget_usdt") or 0.0)
                        current_budget = float(system.oma_registry.get_budget_usdt(market) or 0.0)
                        # Keep current state, update budget only
                        current_state = system.oma_registry.get_state(market) or MarketState.ACTIVE
                        if current_budget <= 0.0:
                            system.oma_registry.set_state(
                                market,
                                current_state,
                                reason=["sniper_budget_restore"],
                                budget_usdt=stored_budget,
                            )
                        elif stored_budget > 0.0 and current_budget > (stored_budget * 1.5):
                            # Normalize budget inflated by the old cumulative-restore bug
                            system.oma_registry.set_state(
                                market,
                                current_state,
                                reason=["sniper_budget_restore_normalize"],
                                budget_usdt=stored_budget,
                            )

                    restored += 1
                    if is_scope:
                        scope_restored_count += 1
                    logger.info(f"[SNIPER] Restored {sniper_id} ({market}) with budget={data.get('budget_usdt')}")
                except Exception as e:
                    logger.warning("[SNIPER] Failed to restore %s: %s", sniper_id, e)
        return restored


# Singleton instance
sniper_store = SniperPositionStore()
