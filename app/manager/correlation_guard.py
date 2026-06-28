# ============================================================
# File: app/manager/correlation_guard.py
# Autocoin OS v3-H — Correlation Guard
# ------------------------------------------------------------
# A gift from this agent's server to the sibling server:
# applies a conviction penalty when entering highly correlated
# coins at the same time.
#
# Two layers:
#   Layer 1: static sector groups (crypto domain knowledge)
#   Layer 2: dynamic outcome correlation (journal EXIT win/loss sync rate)
#
# Core philosophy:
#   - Does NOT "block" entries — softly discourages via conviction penalty
#   - BTC+ETH+SOL all LONG = effectively the same bet 3 times
#   - GOLD (XAUTUSDT) is independent of crypto -> penalty always 0
#   - Opposite direction = hedge -> penalty cancels out
# ============================================================
from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import defaultdict
from typing import Any, Dict, List, Optional, Set, Tuple

from app.core.io_utils import safe_load_json, safe_write_json

logger = logging.getLogger(__name__)

# ── Paths ───────────────────────────────────────────────────
JOURNAL_PATH = os.path.join("runtime", "focus_harpoon_journal.jsonl")
CACHE_PATH = os.path.join("runtime", "correlation_cache.json")

# ── Static correlation groups (Layer 1) ─────────────────────
CORRELATION_GROUPS: Dict[str, Dict[str, Any]] = {
    "BTC_MAJORS": {
        "coins": ["BTCUSDT"],
        "label": "Bitcoin",
        "correlation": 1.0,
    },
    "ETH_ECOSYSTEM": {
        "coins": ["ETHUSDT"],
        "label": "Ethereum ecosystem",
        "correlation": 0.85,
    },
    "LARGE_L1": {
        "coins": ["BTCUSDT", "ETHUSDT", "SOLUSDT"],
        "label": "Large-cap L1",
        "correlation": 0.75,
    },
    "DEFI_BLUE": {
        "coins": ["LINKUSDT", "AAVEUSDT", "UNIUSDT"],
        "label": "DeFi blue chips",
        "correlation": 0.70,
    },
    "ALT_HIGH_BETA": {
        "coins": ["HYPEUSDT", "RAVEUSDT", "ZECUSDT", "TAOUSDT"],
        "label": "High-volatility alts",
        "correlation": 0.50,
    },
    "GOLD": {
        "coins": ["XAUTUSDT"],
        "label": "Gold (independent)",
        "correlation": 0.0,  # uncorrelated with crypto
    },
}

# ── Default correlation for unregistered coins ──────────────
_OTHER_GROUP_CORR = 0.30

# ── Dynamic correlation thresholds ──────────────────────────
_DYNAMIC_SYNC_THRESHOLD = 0.80   # >= 80% sync => high correlation
_DYNAMIC_MIN_OVERLAPS = 3        # need at least 3 overlaps to judge
_DYNAMIC_LOOKBACK_DAYS = 7       # last 7 days
_DYNAMIC_REFRESH_SEC = 3600      # refresh every hour

# ── Penalty limits ──────────────────────────────────────────
# [2026-05-17 100-pt x10] _MAX_PENALTY old -3 -> -30
_MAX_PENALTY = -30
_LARGE_L1_EXTRA_THRESHOLD = 3    # 3+ same-direction in LARGE_L1 -> extra -20 (x10)


def _safe_float(val: Any, default: float = 0.0) -> float:
    """Guard against missing/mistyped journal fields."""
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


class CorrelationGuard:
    """Correlation-based conviction penalty guard.

    Usage:
        from app.manager.correlation_guard import correlation_guard
        result = correlation_guard.check_entry("ETHUSDT", "LONG", positions)
        # result = {"allowed": True, "penalty": -1, "warnings": [...], "overlap_groups": [...]}
    """

    def __init__(self, journal_path: str = JOURNAL_PATH):
        self._journal_path = journal_path
        self._lock = threading.Lock()

        # Layer 1: static groups — reverse index coin->group
        self._coin_to_groups: Dict[str, List[str]] = defaultdict(list)
        for group_name, info in CORRELATION_GROUPS.items():
            for coin in info["coins"]:
                self._coin_to_groups[coin.upper()].append(group_name)

        # Layer 2: dynamic correlation cache
        self._dynamic_pairs: Dict[str, float] = {}  # "COINX|COINY" -> sync_rate
        self._dynamic_ts: float = 0.0

        # Load cache on boot
        self._load_cache()

    # ================================================================
    # Cache I/O
    # ================================================================

    def _load_cache(self) -> None:
        """Load the dynamic correlation cache from disk."""
        data = safe_load_json(CACHE_PATH, default={})
        self._dynamic_pairs = data.get("pairs", {})
        self._dynamic_ts = _safe_float(data.get("ts", 0))
        if self._dynamic_pairs:
            logger.info(
                "[CORR_GUARD] cache loaded: %d pairs, ts=%s",
                len(self._dynamic_pairs),
                time.strftime("%H:%M:%S", time.localtime(self._dynamic_ts)),
            )

    def _save_cache(self) -> None:
        """Save the dynamic correlation cache to disk."""
        data = {
            "pairs": self._dynamic_pairs,
            "ts": self._dynamic_ts,
            "readable_ts": time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(self._dynamic_ts)
            ),
        }
        try:
            safe_write_json(CACHE_PATH, data)
        except Exception as exc:
            logger.warning("[CORR_GUARD] cache save failed: %s", exc)

    # ================================================================
    # Layer 2: dynamic outcome correlation (journal-based)
    # ================================================================

    def _read_journal_exits(self, lookback_days: int = _DYNAMIC_LOOKBACK_DAYS) -> List[Dict]:
        """Extract EXIT records from the last N days."""
        cutoff = time.time() - lookback_days * 86400
        exits: List[Dict] = []

        if not os.path.exists(self._journal_path):
            return exits

        try:
            with open(self._journal_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if rec.get("event") != "EXIT":
                        continue
                    if _safe_float(rec.get("ts")) < cutoff:
                        continue
                    exits.append(rec)
        except OSError as exc:
            logger.warning("[CORR_GUARD] journal read failed: %s", exc)

        return exits

    def _read_journal_entries(self, lookback_days: int = _DYNAMIC_LOOKBACK_DAYS) -> List[Dict]:
        """Extract ENTRY records from the last N days (for overlap timing)."""
        cutoff = time.time() - lookback_days * 86400
        entries: List[Dict] = []

        if not os.path.exists(self._journal_path):
            return entries

        try:
            with open(self._journal_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if rec.get("event") != "ENTRY":
                        continue
                    if _safe_float(rec.get("ts")) < cutoff:
                        continue
                    entries.append(rec)
        except OSError as exc:
            logger.warning("[CORR_GUARD] journal ENTRY read failed: %s", exc)

        return entries

    def refresh_dynamic(self) -> None:
        """Recompute dynamic correlation (based on journal EXIT data).

        Overlap detection:
        - Build holding spans from each coin's ENTRY/EXIT pairs
        - Two coins' holding spans overlapping = an overlap
        - Among overlaps, both won or both lost = synced
        - sync_rate = synced count / total overlap count
        """
        with self._lock:
            now = time.time()
            exits = self._read_journal_exits()
            entries = self._read_journal_entries()

            if not exits:
                self._dynamic_pairs = {}
                self._dynamic_ts = now
                self._save_cache()
                return

            # Match ENTRY/EXIT -> build holding spans
            # {market -> [(entry_ts, exit_ts, won: bool), ...]}
            holdings = self._build_holdings(entries, exits)

            # Analyze overlaps per coin pair
            coins = sorted(holdings.keys())
            new_pairs: Dict[str, float] = {}

            for i, coin_a in enumerate(coins):
                for coin_b in coins[i + 1:]:
                    sync_count, total_overlaps = self._calc_overlap_sync(
                        holdings[coin_a], holdings[coin_b]
                    )
                    if total_overlaps < _DYNAMIC_MIN_OVERLAPS:
                        continue  # not enough data — cannot judge
                    sync_rate = sync_count / total_overlaps
                    pair_key = self._pair_key(coin_a, coin_b)
                    new_pairs[pair_key] = round(sync_rate, 4)

            self._dynamic_pairs = new_pairs
            self._dynamic_ts = now
            self._save_cache()

            high_corr = {k: v for k, v in new_pairs.items() if v >= _DYNAMIC_SYNC_THRESHOLD}
            if high_corr:
                logger.info(
                    "[CORR_GUARD] dynamic correlation refreshed: %d pairs analyzed, %d high-corr %s",
                    len(new_pairs), len(high_corr), high_corr,
                )

    def _build_holdings(
        self,
        entries: List[Dict],
        exits: List[Dict],
    ) -> Dict[str, List[Tuple[float, float, bool]]]:
        """Build holding spans by matching ENTRY/EXIT.

        Returns:
            {market -> [(entry_ts, exit_ts, won), ...]}
        """
        # exit reverse index: (market, direction) -> [exit_records]
        exit_map: Dict[Tuple[str, str], List[Dict]] = defaultdict(list)
        for ex in exits:
            market = ex.get("market", "").upper()
            direction = ex.get("direction", "").upper()
            if market and direction:
                exit_map[(market, direction)].append(ex)

        # Sort each exit list ascending by ts
        for key in exit_map:
            exit_map[key].sort(key=lambda r: _safe_float(r.get("ts")))

        # Match entries with exits (FIFO)
        used_exits: Set[int] = set()  # dedup by exit record id (memory address)
        holdings: Dict[str, List[Tuple[float, float, bool]]] = defaultdict(list)

        for entry in sorted(entries, key=lambda r: _safe_float(r.get("ts"))):
            market = entry.get("market", "").upper()
            direction = entry.get("direction", "").upper()
            entry_ts = _safe_float(entry.get("ts"))
            if not market or not direction or entry_ts <= 0:
                continue

            key = (market, direction)
            for ex in exit_map.get(key, []):
                ex_id = id(ex)
                if ex_id in used_exits:
                    continue
                exit_ts = _safe_float(ex.get("ts"))
                if exit_ts < entry_ts:
                    continue  # this exit belongs to an earlier entry
                pnl = _safe_float(ex.get("pnl_net"))
                won = pnl > 0
                holdings[market].append((entry_ts, exit_ts, won))
                used_exits.add(ex_id)
                break  # FIFO: use the first matching exit

        return dict(holdings)

    @staticmethod
    def _calc_overlap_sync(
        spans_a: List[Tuple[float, float, bool]],
        spans_b: List[Tuple[float, float, bool]],
    ) -> Tuple[int, int]:
        """Analyze holding-span overlaps between two coins.

        Returns:
            (sync_count, total_overlaps)
            sync = both won or both lost
        """
        total = 0
        sync = 0

        for (a_start, a_end, a_won) in spans_a:
            for (b_start, b_end, b_won) in spans_b:
                # span overlap check: max(start) < min(end)
                if max(a_start, b_start) < min(a_end, b_end):
                    total += 1
                    if a_won == b_won:
                        sync += 1

        return sync, total

    @staticmethod
    def _pair_key(coin_a: str, coin_b: str) -> str:
        """Build a sorted pair key (same key regardless of order)."""
        a, b = sorted([coin_a.upper(), coin_b.upper()])
        return f"{a}|{b}"

    def _ensure_dynamic_fresh(self) -> None:
        """Refresh dynamic data if it is stale."""
        if time.time() - self._dynamic_ts > _DYNAMIC_REFRESH_SEC:
            self.refresh_dynamic()

    # ================================================================
    # Group lookup helpers
    # ================================================================

    def _get_groups_for_coin(self, coin: str) -> List[str]:
        """Return the static groups a coin belongs to. Empty list if unregistered."""
        return self._coin_to_groups.get(coin.upper(), [])

    def _is_gold(self, coin: str) -> bool:
        """Check whether the coin is gold (XAUTUSDT)."""
        return coin.upper() == "XAUTUSDT"

    # ================================================================
    # Core API: check_entry
    # ================================================================

    def check_entry(
        self,
        new_coin: str,
        direction: str,
        current_positions: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Correlation check before entry.

        Args:
            new_coin: the coin to enter (e.g. "ETHUSDT")
            direction: "LONG" or "SHORT"
            current_positions: list of currently held positions
                each dict needs at least 'market' and 'direction' fields

        Returns:
            {
                "allowed": True,        # always True (guard does not block)
                "penalty": -2,          # conviction penalty (0 ~ -3)
                "warnings": ["..."],    # list of warning messages
                "overlap_groups": ["LARGE_L1"],  # overlapping group names
            }
        """
        new_coin = new_coin.upper()
        direction = direction.upper()

        result: Dict[str, Any] = {
            "allowed": True,
            "penalty": 0,
            "warnings": [],
            "overlap_groups": [],
        }

        # No positions -> no penalty
        if not current_positions:
            return result

        # Gold is always independent -> penalty 0
        if self._is_gold(new_coin):
            return result

        # Ensure dynamic data is fresh
        self._ensure_dynamic_fresh()

        # ── Layer 1: static group check ──
        new_groups = self._get_groups_for_coin(new_coin)
        penalty = 0
        warnings: List[str] = []
        overlap_groups: Set[str] = set()

        # Count same-direction positions per group
        group_same_dir: Dict[str, int] = defaultdict(int)
        group_opposite_dir: Dict[str, int] = defaultdict(int)

        for pos in current_positions:
            pos_market = pos.get("market", "").upper()
            pos_dir = pos.get("direction", "").upper()

            if not pos_market or not pos_dir:
                continue

            # Skip self (same coin already held)
            if pos_market == new_coin:
                continue

            # Exclude gold positions from correlation
            if self._is_gold(pos_market):
                continue

            # Groups of the existing position
            pos_groups = self._get_groups_for_coin(pos_market)

            # Find groups shared by the new and existing coin
            common_groups = set(new_groups) & set(pos_groups)

            for grp in common_groups:
                overlap_groups.add(grp)
                if pos_dir == direction:
                    group_same_dir[grp] += 1
                else:
                    group_opposite_dir[grp] += 1

            # Unregistered coins are not grouped together under OTHER
            # (distinct unknown coins are treated as independent)

        # ── Compute static group penalty ──
        for grp in overlap_groups:
            same = group_same_dir.get(grp, 0)
            opposite = group_opposite_dir.get(grp, 0)

            if same == 0:
                continue  # no same-direction -> no penalty

            # Opposite direction hedges and cancels out
            net_same = max(0, same - opposite)

            if net_same <= 0:
                warnings.append(
                    f"{CORRELATION_GROUPS[grp]['label']}({grp}): "
                    f"hedge cancelled (same={same}, opposite={opposite})"
                )
                continue

            # [2026-05-17 100-pt x10] same group, same direction: -10 per overlap (old -1)
            grp_penalty = -10 * net_same
            warnings.append(
                f"{CORRELATION_GROUPS[grp]['label']}({grp}): "
                f"{net_same} same-direction -> penalty {grp_penalty}"
            )
            penalty += grp_penalty

            # LARGE_L1 special rule: 3+ same direction -> extra -20 (x10)
            if grp == "LARGE_L1" and net_same >= _LARGE_L1_EXTRA_THRESHOLD:
                penalty += -20
                warnings.append(
                    f"LARGE_L1 overcrowded ({net_same} {direction}) -> extra penalty -2"
                )

        # ── Layer 2: additional dynamic outcome correlation check ──
        dynamic_penalty = self._calc_dynamic_penalty(new_coin, direction, current_positions)
        if dynamic_penalty < 0:
            penalty += dynamic_penalty
            warnings.append(f"dynamic correlation (journal analysis) -> extra penalty {dynamic_penalty}")

        # ── Apply penalty cap ──
        penalty = max(penalty, _MAX_PENALTY)

        result["penalty"] = penalty
        result["warnings"] = warnings
        result["overlap_groups"] = sorted(overlap_groups)

        if penalty < 0:
            logger.info(
                "[CORR_GUARD] %s %s → penalty=%d, groups=%s | %s",
                direction, new_coin, penalty,
                result["overlap_groups"],
                "; ".join(warnings),
            )

        return result

    def _calc_dynamic_penalty(
        self,
        new_coin: str,
        direction: str,
        current_positions: List[Dict[str, Any]],
    ) -> int:
        """Additional penalty based on dynamic correlation.

        If any same-direction existing position has a journal win/loss
        sync rate of 80% or higher, apply -1.
        """
        if not self._dynamic_pairs:
            return 0

        new_coin = new_coin.upper()
        penalty = 0

        for pos in current_positions:
            pos_market = pos.get("market", "").upper()
            pos_dir = pos.get("direction", "").upper()

            if not pos_market or pos_market == new_coin:
                continue
            if pos_dir != direction:
                continue  # opposite direction is irrelevant

            pair_key = self._pair_key(new_coin, pos_market)
            sync_rate = self._dynamic_pairs.get(pair_key, 0.0)

            if sync_rate >= _DYNAMIC_SYNC_THRESHOLD:
                penalty -= 10  # [2026-05-17 100-pt x10] -10 per high-sync pair (old -1)

        return penalty

    # ================================================================
    # Exposure map
    # ================================================================

    def get_exposure_map(
        self,
        positions: List[Dict[str, Any]],
    ) -> Dict[str, Dict[str, Any]]:
        """Analyze current positions' exposure per group.

        Returns:
            {
                "LARGE_L1": {
                    "count": 3,
                    "direction": "LONG",
                    "coins": ["BTCUSDT", "ETHUSDT", "SOLUSDT"],
                    "risk_level": "HIGH",
                },
                ...
            }
        """
        # Collect coins + directions per group
        group_data: Dict[str, Dict[str, List[str]]] = defaultdict(
            lambda: defaultdict(list)
        )

        for pos in positions:
            market = pos.get("market", "").upper()
            direction = pos.get("direction", "").upper()
            if not market or not direction:
                continue

            groups = self._get_groups_for_coin(market)
            if not groups:
                # Unregistered coins go to OTHER
                groups = ["OTHER"]

            for grp in groups:
                group_data[grp][direction].append(market)

        # Build the result
        exposure: Dict[str, Dict[str, Any]] = {}

        for grp, dir_map in group_data.items():
            long_coins = dir_map.get("LONG", [])
            short_coins = dir_map.get("SHORT", [])
            total = len(long_coins) + len(short_coins)

            # Determine the dominant direction
            if len(long_coins) > len(short_coins):
                main_dir = "LONG"
                same_count = len(long_coins)
            elif len(short_coins) > len(long_coins):
                main_dir = "SHORT"
                same_count = len(short_coins)
            else:
                main_dir = "MIXED"
                same_count = max(len(long_coins), len(short_coins))

            # Determine the risk level
            if long_coins and short_coins:
                risk_level = "HEDGED"
            elif same_count >= 3:
                risk_level = "HIGH"
            elif same_count == 2:
                risk_level = "MEDIUM"
            else:
                risk_level = "LOW"

            all_coins = sorted(set(long_coins + short_coins))

            exposure[grp] = {
                "count": total,
                "direction": main_dir,
                "coins": all_coins,
                "long_count": len(long_coins),
                "short_count": len(short_coins),
                "risk_level": risk_level,
            }

        return exposure

    # ================================================================
    # Correlation matrix (for API display)
    # ================================================================

    def get_correlation_matrix(self) -> Dict[str, Any]:
        """Return a combined static + dynamic correlation matrix.

        For the API dashboard.
        """
        self._ensure_dynamic_fresh()

        # Correlation between static groups
        static_groups = {}
        for grp_name, info in CORRELATION_GROUPS.items():
            static_groups[grp_name] = {
                "label": info["label"],
                "coins": info["coins"],
                "base_correlation": info["correlation"],
            }

        # Dynamic pair correlation (top 20 only)
        dynamic_top = dict(
            sorted(
                self._dynamic_pairs.items(),
                key=lambda x: x[1],
                reverse=True,
            )[:20]
        )

        return {
            "static_groups": static_groups,
            "dynamic_pairs": dynamic_top,
            "dynamic_pair_count": len(self._dynamic_pairs),
            "dynamic_high_corr_count": sum(
                1 for v in self._dynamic_pairs.values()
                if v >= _DYNAMIC_SYNC_THRESHOLD
            ),
            "last_refresh": self._dynamic_ts,
            "last_refresh_readable": time.strftime(
                "%Y-%m-%d %H:%M:%S",
                time.localtime(self._dynamic_ts),
            ) if self._dynamic_ts > 0 else "never",
        }

    # ================================================================
    # Status query (API)
    # ================================================================

    def get_status(self) -> Dict[str, Any]:
        """Return the full guard status."""
        return {
            "static_groups": {
                grp: {
                    "label": info["label"],
                    "coins": info["coins"],
                    "correlation": info["correlation"],
                }
                for grp, info in CORRELATION_GROUPS.items()
            },
            "dynamic": {
                "pair_count": len(self._dynamic_pairs),
                "high_corr_pairs": {
                    k: v for k, v in self._dynamic_pairs.items()
                    if v >= _DYNAMIC_SYNC_THRESHOLD
                },
                "last_refresh_ts": self._dynamic_ts,
                "last_refresh": time.strftime(
                    "%Y-%m-%d %H:%M:%S",
                    time.localtime(self._dynamic_ts),
                ) if self._dynamic_ts > 0 else "never",
                "refresh_interval_sec": _DYNAMIC_REFRESH_SEC,
            },
            "config": {
                "max_penalty": _MAX_PENALTY,
                "large_l1_extra_threshold": _LARGE_L1_EXTRA_THRESHOLD,
                "dynamic_sync_threshold": _DYNAMIC_SYNC_THRESHOLD,
                "dynamic_min_overlaps": _DYNAMIC_MIN_OVERLAPS,
                "dynamic_lookback_days": _DYNAMIC_LOOKBACK_DAYS,
                "other_group_correlation": _OTHER_GROUP_CORR,
            },
        }


# ── Singleton ────────────────────────────────────────────────
correlation_guard = CorrelationGuard()
