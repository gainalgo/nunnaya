# ============================================================
# Trade Journal — dedicated trade ledger for FOCUS + Harpoon
# ------------------------------------------------------------
# Records every entry/exit to a JSONL file.
# Each record includes strategy name, direction, price, qty, PnL, fee, and reason.
#
# Usage:
#   from app.manager.trade_journal import journal
#   journal.record_entry(...)
#   journal.record_exit(...)
#   journal.get_trades(limit=50)
#   journal.get_summary()
# ============================================================
from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

JOURNAL_PATH = os.path.join("runtime", "focus_harpoon_journal.jsonl")

# Bybit taker fee (one-way)
FEE_RATE = 0.00055

# ── EXIT dedup guard ──
# Prevents the bug where the phantom EXIT path (_check_bybit_positions) and the SYNC
# path (_live_sync) each record the same SERVER_SL exit (triple-recording observed on
# BCHUSDT 2026-04-23).
# If the same (market, direction, entry_price, reason) is re-called within the window, skip.
# cooldown_sec (default 600s) blocks re-entry/re-exit, so a 30s window never blocks normal trades.
EXIT_DEDUP_WINDOW_SEC = 30.0
_EXIT_DEDUP_CACHE_CAP = 200


@dataclass
class TradeRecord:
    """A single trade record."""
    ts: float = 0.0                 # record time (unix timestamp)
    strategy: str = ""              # "FOCUS" or "HARPOON"
    event: str = ""                 # "ENTRY" / "EXIT" / "PARTIAL"
    market: str = ""
    direction: str = ""             # "LONG" or "SHORT"
    price: float = 0.0              # fill price
    qty: float = 0.0
    # Exit-only fields
    entry_price: float = 0.0
    exit_reason: str = ""           # "TP1", "TP2", "SL", "TIMEOUT", "trend_reversal", etc.
    pnl_gross: float = 0.0          # PnL before fees ($)
    fee: float = 0.0                # fee ($)
    pnl_net: float = 0.0            # net PnL ($)
    pnl_pct: float = 0.0            # return (%)
    hold_sec: float = 0.0           # hold time (seconds)
    leverage: int = 1
    margin: float = 0.0             # margin used ($)
    roe_pct: float = 0.0            # Return on Equity (%)
    # Extra info
    phase: str = ""                 # FOCUS: SCOUT/REINFORCED/ALLIN
    dynamic_trailing: bool = False  # whether Dynamic Trailing was used
    breakeven_locked: bool = False  # whether breakeven is locked
    peak_profit_pct: float = 0.0    # peak return (%)


class TradeJournal:
    """Dedicated ledger manager for FOCUS + Harpoon."""

    def __init__(self, path: Optional[str] = None):
        # ★ [2026-06-23] path-aware — per-exchange ledger isolation (Bybit=default JOURNAL_PATH, Binance=separate).
        #   If path is unset, use the existing global path (=Bybit/Harpoon, no behavior change).
        self.path = path or JOURNAL_PATH
        self._lock = threading.Lock()
        self._recent_exits: Dict[tuple, float] = {}
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        # ★ In-memory cache (2026-05-15 leak fix) ──
        # Stops the leak where dashboard polling fully reparsed a 38MB file every 5s.
        # Loads file→list once on first call. Afterwards _append/get_* use memory only.
        self._cache: List[Dict] = []
        self._cache_loaded = False

    def _ensure_cache(self):
        """★ On first call, load the whole file → in-memory list (once only)."""
        if self._cache_loaded:
            return
        with self._lock:
            if self._cache_loaded:
                return
            try:
                if os.path.exists(self.path):
                    with open(self.path, "r", encoding="utf-8") as f:
                        for line in f:
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                self._cache.append(json.loads(line))
                            except json.JSONDecodeError:
                                continue
                logger.info("[JOURNAL] cache loaded: %d records", len(self._cache))
            except OSError as exc:
                logger.warning("[JOURNAL] cache load failed: %s", exc)
            self._cache_loaded = True

    def _append(self, record: TradeRecord):
        """Append one JSONL line + update the in-memory cache."""
        with self._lock:
            rec_dict = asdict(record)
            try:
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(rec_dict, ensure_ascii=False) + "\n")
                    f.flush()
                    os.fsync(f.fileno())
            except OSError as exc:
                logger.warning("[JOURNAL] write failed: %s", exc)
            # ★ Cache update (leak fix 2026-05-15) — the key to avoiding a full reparse each time
            if self._cache_loaded:
                self._cache.append(rec_dict)

    # ── Record Blocked (a blocked entry attempt) ──

    def record_blocked(
        self,
        strategy: str,
        market: str,
        direction: str,
        reason: str,
        detail: str = "",
    ):
        """Record a blocked entry attempt — for later 'how many times did we block' analysis."""
        rec = TradeRecord(
            ts=time.time(),
            strategy=strategy.upper(),
            event="BLOCKED",
            market=market,
            direction=direction,
            exit_reason=reason,
            phase=detail,
        )
        self._append(rec)
        logger.info("[JOURNAL] %s BLOCKED %s %s | %s | %s",
                    strategy, direction, market, reason, detail)
        # ★ [2026-06-15 solution B] gate-aggregation sink (optional, registered by FocusManager).
        #   A single funnel for all BLOCKED events, giving a complete 'why did it stay silent'
        #   tally. Failures are never propagated into the recording flow.
        _sink = getattr(self, "_gate_sink", None)
        if _sink is not None:
            try:
                _sink(strategy, market, direction, reason)
            except Exception:  # noqa: BLE001
                pass

    # ── Record Entry ──

    def record_entry(
        self,
        strategy: str,
        market: str,
        direction: str,
        price: float,
        qty: float,
        leverage: int = 1,
        phase: str = "",
    ):
        """Record an entry."""
        margin = price * qty / leverage if leverage > 0 else price * qty
        rec = TradeRecord(
            ts=time.time(),
            strategy=strategy.upper(),
            event="ENTRY",
            market=market,
            direction=direction,
            price=price,
            qty=qty,
            leverage=leverage,
            margin=round(margin, 4),
            phase=phase,
        )
        self._append(rec)
        logger.info("[JOURNAL] %s ENTRY %s %s @ $%.2f qty=%.4f margin=$%.2f",
                    strategy, direction, market, price, qty, margin)

    # ── Record Exit ──

    def record_exit(
        self,
        strategy: str,
        market: str,
        direction: str,
        entry_price: float,
        exit_price: float,
        qty: float,
        reason: str,
        leverage: int = 1,
        hold_sec: float = 0.0,
        phase: str = "",
        dynamic_trailing: bool = False,
        breakeven_locked: bool = False,
        peak_profit_pct: float = 0.0,
    ):
        """Record an exit (fee calculated automatically)."""
        # ── Dedup guard — prevent duplicate records when phantom/SYNC paths fire together ──
        _now = time.time()
        _dedup_key = (
            market.upper(),
            direction.upper(),
            round(float(entry_price), 8),
            reason,
        )
        with self._lock:
            _last_ts = self._recent_exits.get(_dedup_key)
            if _last_ts is not None and (_now - _last_ts) < EXIT_DEDUP_WINDOW_SEC:
                logger.warning(
                    "[JOURNAL] EXIT dedup: %s %s %s | %s (prev %.1fs ago) — skipped duplicate",
                    strategy.upper(), direction.upper(), market, reason, _now - _last_ts,
                )
                return
            self._recent_exits[_dedup_key] = _now
            # Cache cleanup — drop entries older than window ×10
            if len(self._recent_exits) > _EXIT_DEDUP_CACHE_CAP:
                _cutoff = _now - EXIT_DEDUP_WINDOW_SEC * 10
                self._recent_exits = {
                    k: v for k, v in self._recent_exits.items() if v > _cutoff
                }

        # PnL calculation
        if direction.upper() == "LONG":
            pnl_gross = (exit_price - entry_price) * qty
        else:
            pnl_gross = (entry_price - exit_price) * qty

        fee = (entry_price * qty + exit_price * qty) * FEE_RATE
        pnl_net = pnl_gross - fee

        pnl_pct = ((exit_price / entry_price - 1) * 100) if direction.upper() == "LONG" else ((1 - exit_price / entry_price) * 100)

        margin = entry_price * qty / leverage if leverage > 0 else entry_price * qty
        roe_pct = (pnl_net / margin * 100) if margin > 0 else 0.0

        rec = TradeRecord(
            ts=time.time(),
            strategy=strategy.upper(),
            event="EXIT",
            market=market,
            direction=direction,
            price=exit_price,
            qty=qty,
            entry_price=entry_price,
            exit_reason=reason,
            pnl_gross=round(pnl_gross, 4),
            fee=round(fee, 4),
            pnl_net=round(pnl_net, 4),
            pnl_pct=round(pnl_pct, 4),
            hold_sec=round(hold_sec, 1),
            leverage=leverage,
            margin=round(margin, 4),
            roe_pct=round(roe_pct, 2),
            phase=phase,
            dynamic_trailing=dynamic_trailing,
            breakeven_locked=breakeven_locked,
            peak_profit_pct=round(peak_profit_pct, 4),
        )
        self._append(rec)
        logger.info(
            "[JOURNAL] %s EXIT %s %s @ $%.2f→$%.2f | net=$%.4f (%.2f%%) ROE=%.1f%% | %s",
            strategy, direction, market, entry_price, exit_price,
            pnl_net, pnl_pct, roe_pct, reason,
        )

    # ── Record Partial ──

    def record_partial(
        self,
        strategy: str,
        market: str,
        direction: str,
        entry_price: float,
        exit_price: float,
        qty: float,
        reason: str,
        leverage: int = 1,
    ):
        """Record a partial exit."""
        if direction.upper() == "LONG":
            pnl_gross = (exit_price - entry_price) * qty
        else:
            pnl_gross = (entry_price - exit_price) * qty

        fee = (entry_price * qty + exit_price * qty) * FEE_RATE
        pnl_net = pnl_gross - fee
        margin = entry_price * qty / leverage if leverage > 0 else entry_price * qty

        rec = TradeRecord(
            ts=time.time(),
            strategy=strategy.upper(),
            event="PARTIAL",
            market=market,
            direction=direction,
            price=exit_price,
            qty=qty,
            entry_price=entry_price,
            exit_reason=reason,
            pnl_gross=round(pnl_gross, 4),
            fee=round(fee, 4),
            pnl_net=round(pnl_net, 4),
            leverage=leverage,
            margin=round(margin, 4),
        )
        self._append(rec)

    # ── Query ──

    def get_trades(
        self,
        limit: int = 50,
        strategy: str = "",
        market: str = "",
        include_blocked: bool = True,
        page: int = 1,
    ) -> Dict:
        """Query trades (pagination + filter). ★ Cache-based (leak fix 2026-05-15)."""
        self._ensure_cache()
        # Filtering — iterate the cache list (no file read)
        if strategy or market or not include_blocked:
            strat_u = strategy.upper() if strategy else ""
            mkt_u = market.upper() if market else ""
            trades: List[Dict] = []
            for rec in self._cache:
                if strat_u and rec.get("strategy") != strat_u:
                    continue
                if mkt_u and rec.get("market") != mkt_u:
                    continue
                if not include_blocked and rec.get("event") == "BLOCKED":
                    continue
                trades.append(rec)
        else:
            trades = self._cache  # no filter → reference only (no copy)

        total_count = len(trades)
        # Page slicing (stored oldest-first → page=1 is newest)
        end_idx = total_count - ((page - 1) * limit)
        start_idx = max(0, end_idx - limit)
        end_idx = max(0, end_idx)
        return {"trades": trades[start_idx:end_idx], "total_count": total_count}

    def get_markets(self) -> List[str]:
        """Get the list of unique markets. ★ Cache-based."""
        self._ensure_cache()
        markets = {rec.get("market") for rec in self._cache if rec.get("market")}
        return sorted(markets)

    def get_summary(self) -> Dict[str, Any]:
        """Per-strategy performance summary."""
        trades = self.get_trades(limit=9999, include_blocked=True)["trades"]
        exits = [t for t in trades if t.get("event") == "EXIT"]

        # ★ Today reset timestamp (07:00 KST = 22:00 UTC)
        import datetime as _dt
        now_utc = _dt.datetime.now(_dt.timezone.utc)
        _reset_h = 22
        _today_reset = now_utc.replace(hour=_reset_h, minute=0, second=0, microsecond=0)
        if now_utc.hour < _reset_h:
            _today_reset -= _dt.timedelta(days=1)
        _reset_ts = _today_reset.timestamp()

        summary = {}
        for strat in ("FOCUS", "HARPOON"):
            s_exits = [t for t in exits if t.get("strategy") == strat]
            wins = [t for t in s_exits if t.get("pnl_net", 0) > 0]
            losses = [t for t in s_exits if t.get("pnl_net", 0) <= 0]
            total_pnl = sum(t.get("pnl_net", 0) for t in s_exits)
            total_fee = sum(t.get("fee", 0) for t in s_exits)

            # Performance when Dynamic trailing was used
            dt_exits = [t for t in s_exits if t.get("dynamic_trailing")]
            dt_pnl = sum(t.get("pnl_net", 0) for t in dt_exits)
            no_dt_exits = [t for t in s_exits if not t.get("dynamic_trailing")]
            no_dt_pnl = sum(t.get("pnl_net", 0) for t in no_dt_exits)

            win_pnl = sum(t.get("pnl_net", 0) for t in wins)
            loss_pnl = sum(t.get("pnl_net", 0) for t in losses)

            summary[strat] = {
                "total_trades": len(s_exits),
                "wins": len(wins),
                "losses": len(losses),
                "win_rate": round(len(wins) / len(s_exits) * 100, 1) if s_exits else 0,
                "total_pnl": round(total_pnl, 4),
                "total_fee": round(total_fee, 4),
                "avg_pnl": round(total_pnl / len(s_exits), 4) if s_exits else 0,
                "best_trade": round(max((t.get("pnl_net", 0) for t in s_exits), default=0), 4),
                "worst_trade": round(min((t.get("pnl_net", 0) for t in s_exits), default=0), 4),
                # Amount-based win rate: win_pnl / (win_pnl + |loss_pnl|) × 100
                "win_pnl": round(win_pnl, 4),
                "loss_pnl": round(loss_pnl, 4),
                # Dynamic trailing comparison
                "dt_trades": len(dt_exits),
                "dt_pnl": round(dt_pnl, 4),
                "no_dt_trades": len(no_dt_exits),
                "no_dt_pnl": round(no_dt_pnl, 4),
                # ★ Today PnL (07:00 KST reset)
                "today_pnl": round(sum(t.get("pnl_net", 0) for t in s_exits if t.get("ts", 0) >= _reset_ts), 4),
                "today_trades": sum(1 for t in s_exits if t.get("ts", 0) >= _reset_ts),
            }

        # ★ [2026-05-31 owner] Clarify the PnL baseline — first EXIT ts (journal file start point).
        #   Owner's insight: "an accurate PnL baseline would be nice, but it's actually ambiguous"
        #   → solved by showing a 'since' marker.
        first_exit_ts = min((t.get("ts", 0) for t in exits if t.get("ts", 0) > 0), default=0)

        summary["combined"] = {
            "total_pnl": round(sum(s.get("total_pnl", 0) for s in summary.values() if isinstance(s, dict)), 4),
            "total_trades": sum(s.get("total_trades", 0) for s in summary.values() if isinstance(s, dict)),
            "total_fee": round(sum(s.get("total_fee", 0) for s in summary.values() if isinstance(s, dict)), 4),
            "today_pnl": round(sum(s.get("today_pnl", 0) for s in summary.values() if isinstance(s, dict)), 4),
            "today_trades": sum(s.get("today_trades", 0) for s in summary.values() if isinstance(s, dict)),
            "first_exit_ts": first_exit_ts,  # ★ PnL baseline (journal's first EXIT)
        }

        return summary


# ── Singleton (Bybit/Harpoon global ledger — no behavior change) ──
journal = TradeJournal()

# ── Per-path registry (per-exchange ledger isolation) ──
#   get_journal(JOURNAL_PATH) returns the global singleton above as-is (preserving the same
#   in-memory cache/dedup/gate_sink).
#   Other paths (e.g. runtime/binance_futures/journal.jsonl) cache and share one dedicated instance.
_JOURNALS: Dict[str, TradeJournal] = {JOURNAL_PATH: journal}
_JOURNALS_LOCK = threading.Lock()


def get_journal(path: Optional[str] = None) -> TradeJournal:
    p = path or JOURNAL_PATH
    j = _JOURNALS.get(p)
    if j is None:
        with _JOURNALS_LOCK:
            j = _JOURNALS.get(p)
            if j is None:
                j = TradeJournal(p)
                _JOURNALS[p] = j
    return j
