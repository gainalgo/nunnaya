# ============================================================
# FOCUS Strategy API Router
# ------------------------------------------------------------
# REST endpoints for controlling the FOCUS strategy manager.
# ============================================================
from __future__ import annotations

import logging
import os
import threading
from typing import Any, Dict, Optional

from fastapi import APIRouter, Query, Request

logger = logging.getLogger(__name__)

# Binance USDT-M futures FOCUS API — mirror of strategy_focus_router (manager is binance_futures_manager only).
router = APIRouter(prefix="/api/strategy/binance_futures", tags=["BINANCE_FUT"])

# ★ [2026-06-23 audit bug#5] Per-exchange ledger isolation — all journal reads in this router come from the Binance-only ledger.
#   (Separate from the Bybit router / global singleton. Same path as binance_futures_manager._journal_path.)
_BINANCE_FUT_JOURNAL_PATH = os.path.join("runtime", "binance_futures", "journal.jsonl")
_BINANCE_FUT_SNAP_DIR = os.path.join("runtime", "binance_futures", "daily_snapshots")  # Isolated daily PnL chart
_BINANCE_FUT_CFGSNAP_DIR = os.path.join("runtime", "binance_futures", "config_snapshots")  # Config change history
_BINANCE_FUT_CAP_EVENTS = os.path.join("runtime", "binance_futures", "capital_events.jsonl")
_BINANCE_FUT_CAP_BASELINE = os.path.join("runtime", "binance_futures", "capital_baseline.json")


def _bjournal():
    from app.manager.trade_journal import get_journal as _get_journal
    return _get_journal(_BINANCE_FUT_JOURNAL_PATH)


def _bcapital():
    """Binance-only capital tracker (deposit/withdraw / ROI isolation)."""
    from app.manager.capital_tracker import get_capital_tracker
    return get_capital_tracker(_BINANCE_FUT_CAP_EVENTS, _BINANCE_FUT_CAP_BASELINE)


def _bcoincard(lookback_days: int = 30):
    """Binance-only coin report card (derived from the binance journal — fills in as trades accumulate)."""
    from app.manager.coin_report_card import CoinReportCard
    return CoinReportCard(journal_path=_BINANCE_FUT_JOURNAL_PATH, lookback_days=lookback_days)


def _binance_equity(fm):
    """Binance futures account (equity_usdt, cash_usdt). paper goes via FocusDryClient (virtual/real balance)."""
    client = fm._get_client()
    eq = 0.0
    cash = 0.0
    try:
        cash = float(client.get_available_margin() or 0)
    except Exception:
        pass
    try:
        for a in client.accounts():
            if a.get("currency") == "USDT":
                eq = float(a.get("balance", 0) or 0) + float(a.get("locked", 0) or 0)
                break
    except Exception:
        pass
    if eq <= 0:
        eq = cash
    return eq, cash


@router.get("/system-status")
def binance_system_status(request: Request) -> Dict[str, Any]:
    """For the Overall Status card — the shared system snapshot (regime/event/tick etc.) is kept as-is,
    only total equity and session PnL are replaced with the *Binance futures* account/ledger values (per-exchange split).
    A shim rewrites /api/system/status → this endpoint (?ex=binance_futures)."""
    system = request.app.state.system
    snap = dict(system.status())
    fm = _get_fm(request)
    try:
        eq, cash = _binance_equity(fm)
        e = dict(snap.get("equity") or {})
        e["equity_usdt"] = eq
        e["cash_usdt"] = cash
        e["deployed_usdt"] = max(0.0, eq - cash)
        snap["equity"] = e
        # Session PnL = cumulative realized PnL from the Binance-only ledger (per-exchange split)
        _summ = _bjournal().get_summary().get("combined") or {}
        snap["session_pnl"] = float(_summ.get("total_pnl") or 0.0)
        snap["exchange_label"] = "BINANCE USDT-M"
    except Exception as exc:
        logger.warning("[BINANCE_FUT_API] system-status equity override failed: %s", exc)
        # ★ [audit medium#6] Block fail-open leak — if the override fails, snap's Bybit equity/session_pnl
        #   would remain, showing another exchange's balance in the Binance window. Conservatively overwrite with 'unavailable (None)'.
        snap["equity"] = {"equity_usdt": None, "cash_usdt": None, "deployed_usdt": None}
        snap["session_pnl"] = None
        snap["exchange_label"] = "BINANCE USDT-M"
    try:
        snap["api_stats"] = fm._get_client().get_api_stats()
    except Exception:
        pass
    return {"ok": True, "system": snap}


_FM_LOCK = threading.Lock()


def _get_fm(request: Request):
    """Get BinanceFuturesManager from system (create if missing).
    ★ [2026-06-23] double-checked lock — when the dashboard calls several APIs concurrently, _get_fm did a
    lockless check-then-create, so the manager could be created N times (seeding 779 fields N times, log spam). This blocks that race."""
    system = request.app.state.system
    fm = getattr(system, "binance_futures_manager", None)
    if fm is None:
        with _FM_LOCK:
            fm = getattr(system, "binance_futures_manager", None)  # re-check inside the lock
            if fm is None:
                from app.manager.binance_futures_manager import BinanceFuturesManager
                fm = BinanceFuturesManager(system=system)
                system.binance_futures_manager = fm
    return fm


def _bot_opinion(signal: str, trend: str, gs_total, gs_threshold,
                 block_reason: str, pa_pattern: str) -> dict | None:
    """[2026-05-28 owner] Warning-badge data for when the bot disagrees with the surface signal.

    Yesterday's XLM LONG case: score was 100 but internally the bot tried a SHORT flip due to BB 129% overheating.
    On manual entry the `_is_manual` conflict guard blocked it, but the owner could not see *why* it was blocked
    in the Dashboard. This function surfaces the intent/internal-opinion conflict up front.

    return None means no badge. If a dict:
        level: "warn" (red) | "info" (yellow)
        text: one-line message
    """
    br = (block_reason or "")
    pa = (pa_pattern or "")
    try:
        gst = float(gs_total) if gs_total is not None else None
        gth = float(gs_threshold) if gs_threshold is not None else 65.0
    except Exception:
        gst, gth = None, 65.0

    # Priority 1: BB extreme → recommend opposite-direction flip (strongest conflict)
    if signal == "BUY":
        if "FLIP BUY" in br or "overbought" in br:
            return {"level": "warn", "text": "⚠ BB overheated — SHORT recommended"}
        if "BOS_BEARISH" in br or "BOS_BEARISH" in pa:
            return {"level": "warn", "text": "⚠ Bearish BOS + BB oversold — LONG risky"}
        if trend == "DOWNTREND":
            return {"level": "warn", "text": "⚠ H4 downtrend — LONG unsuitable"}
    elif signal == "SELL":
        if "FLIP SELL" in br or "oversold" in br:
            return {"level": "warn", "text": "⚠ BB extreme — LONG recommended"}
        if "BOS_BULLISH" in br or "BOS_BULLISH" in pa:
            return {"level": "warn", "text": "⚠ Bullish BOS — SHORT risky"}
        if trend == "UPTREND":
            return {"level": "warn", "text": "⚠ H4 uptrend — SHORT unsuitable"}

    # Priority 2: 30M direction conflict
    if "30M dir conflict" in br or "30M UPTREND" in br or "30M DOWNTREND" in br:
        return {"level": "warn", "text": "⚠ 30M direction conflict"}

    # Priority 3: negative score (strongly weak)
    if gst is not None and gst < 0 and signal in ("BUY", "SELL"):
        return {"level": "info", "text": f"🔸 Negative score ({int(gst)}) — entry very weak"}

    # Priority 4: score below threshold but positive (near)
    if gst is not None and 0 <= gst < gth and signal in ("BUY", "SELL"):
        if gst >= gth * 0.7:  # within 70%+ of threshold
            return {"level": "info", "text": f"🔸 Score near ({int(gst)}/{int(gth)})"}

    return None


# ── Status ──────────────────────────────────────────────────

# /status response cache — get_status() is heavy on every call: per-position current-price fetch + _get_b12_breadth_vote
# (full scan of the journal cache, no result TTL) + today_pnl, etc. If the dashboard polls every 1-2s across multiple tabs,
# concurrent load + the per-host 6-connection browser queue stalls → 4.5s timeout → canceled → the whole FOCUS panel hangs on "loading status".
# get_status(self) is request-independent (fleet-global), so a short TTL lets tabs share one computation.
# [2026-06-19 owner "focus loads as a whole on every server"]. Same pattern as peer-cache (cd7a2b9).
_STATUS_RESP_BOX: dict = {"ts": 0.0, "data": None}
_STATUS_RESP_TTL = 3.0   # seconds — short since it's PnL/position display (3s staleness is fine). Hangs are from concurrent load, solved by dedup alone.


@router.get("/status")
def focus_status(request: Request):
    """Current FOCUS state, position, PnL, daily discipline.
    ★ Caches the response for _STATUS_RESP_TTL seconds → multi-tab/multi-poll reuse one heavy get_status() call (prevents a full hang)."""
    import time as _t
    now = _t.time()
    _box = _STATUS_RESP_BOX
    if _box.get("data") is not None and (now - float(_box.get("ts") or 0.0)) < _STATUS_RESP_TTL:
        return _box["data"]
    fm = _get_fm(request)
    out = {"ok": True, **fm.get_status()}
    _STATUS_RESP_BOX["ts"] = now
    _STATUS_RESP_BOX["data"] = out
    return out


# ── Enable / Disable ────────────────────────────────────────

@router.post("/enable")
def focus_enable(
    request: Request,
    budget_usdt: Optional[float] = Query(None, ge=0, description="FOCUS budget in USDT (0=auto). Omit to keep current."),
    leverage: Optional[int] = Query(None, ge=1, le=100, description="Leverage multiplier. Omit to keep current."),
    direction_mode: Optional[str] = Query(None, description="long_only / short_only / both. Omit to keep current."),
):
    """Enable FOCUS strategy — only overwrite fields that are explicitly sent."""
    fm = _get_fm(request)
    cfg: Dict[str, Any] = {"enabled": True}
    if budget_usdt is not None:
        cfg["budget_usdt"] = budget_usdt
    if leverage is not None:
        cfg["leverage"] = leverage
    if direction_mode is not None:
        cfg["direction_mode"] = direction_mode
    fm.update_config(cfg)
    logger.info("[BINANCE_FUT_API] Enabled: overrides=%s", {k: v for k, v in cfg.items() if k != "enabled"} or "none")
    return {"ok": True, "enabled": True, **fm.get_status()}


@router.post("/disable")
def focus_disable(
    request: Request,
    close_position: bool = Query(False, description="Close open position before disabling"),
):
    """Disable FOCUS strategy."""
    fm = _get_fm(request)

    # ★ H8 FIX: multi-position handling — close if position or positions exist
    if close_position and (fm.position or fm.positions):
        try:
            fm._execute_exit("manual_disable", is_sl=False)
        except Exception as exc:
            logger.warning("[BINANCE_FUT_API] Position close on disable failed: %s", exc)

    fm.update_config({"enabled": False})
    logger.info("[BINANCE_FUT_API] Disabled (close_position=%s)", close_position)
    return {"ok": True, "enabled": False}


@router.post("/close-all")
def focus_close_all(request: Request):
    """Close ALL FOCUS positions on Bybit and clear local state.
    ★ Queries the actual Bybit positions and closes them all (prevents ghost positions)."""
    fm = _get_fm(request)
    closed = []
    errors = []

    # ★ Refresh the re-entry cooldown — prevents the Scanner from re-entering immediately after close-all
    import time as _time
    fm._last_exit_ts = _time.time()
    if fm.positions:
        fm._last_exit_market = fm.positions[0].market
        fm._last_exit_direction = fm.positions[0].direction

    # 1. Close positions in the local positions list — use _close_position (includes journal recording)
    for pos in list(fm.positions):
        try:
            ok = fm._close_position(pos, reason="manual_close_all")
            if ok:
                closed.append({"market": pos.market, "direction": pos.direction, "qty": pos.qty})
                logger.info("[BINANCE_FUT_API] Closed %s %s qty=%.4f", pos.direction, pos.market, pos.qty)
            else:
                errors.append({"market": pos.market, "error": "close_failed"})
        except Exception as exc:
            errors.append({"market": pos.market, "error": str(exc)})
            logger.warning("[BINANCE_FUT_API] Close failed %s: %s", pos.market, exc)

    # 2. Legacy single position
    if fm.position and fm.position.market:
        try:
            ok = fm._close_position(fm.position, reason="manual_close_all")
            if ok:
                closed.append({"market": fm.position.market, "direction": fm.position.direction, "qty": fm.position.qty})
            else:
                errors.append({"market": fm.position.market, "error": "close_failed"})
        except Exception as exc:
            errors.append({"market": fm.position.market, "error": str(exc)})

    # 3. ★ Query all actual open positions on the exchange → close orphan positions (client seam = Binance)
    try:
        import time as _t
        open_positions = [p for p in fm._get_client().list_open_positions()
                          if float(p.get('size', 0) or 0) > 0]

        if open_positions:
            logger.warning("[BINANCE_FUT_API] Found %d orphan positions on exchange — closing all", len(open_positions))

        for bp in open_positions:
            sym = bp.get('symbol', '')
            side = bp.get('side', '')
            size = bp.get('size', '0')
            close_side = 'Sell' if side == 'Buy' else 'Buy'
            try:
                fm._get_client().place_order(
                    market=sym, side=close_side, ord_type="market", volume=float(size), reduce_only=True)
                upl = float(bp.get('unrealisedPnl', 0) or 0)
                closed.append({"market": sym, "direction": side, "qty": float(size), "uPnL": round(upl, 2), "orphan": True})
                logger.info("[BINANCE_FUT_API] Orphan closed: %s %s qty=%s", sym, side, size)
            except Exception as exc:
                errors.append({"market": sym, "error": str(exc), "orphan": True})
                logger.error("[BINANCE_FUT_API] Orphan close FAILED %s: %s", sym, exc)
            _t.sleep(0.3)
    except Exception as exc:
        logger.error("[BINANCE_FUT_API] exchange position scan failed: %s", exc)
        errors.append({"market": "EXCHANGE_SCAN", "error": str(exc)})

    # 4. ★ If the exchange scan fails, keep local state (prevents orphans)
    from app.manager.focus_manager import FocusState
    bybit_scan_failed = any(e.get("market") == "EXCHANGE_SCAN" for e in errors)
    if bybit_scan_failed and not closed:
        # Bybit connection itself failed → keep local state (cleaned up in next sync)
        logger.warning("[BINANCE_FUT_API] Bybit scan failed — keeping local state to prevent orphans")
    else:
        fm.positions = []
        fm.position = None
        fm.state = FocusState.DORMANT
        fm.selected_market = ""
        fm.selected_direction = ""
        fm.daily_plans_used = 0
        fm._save_config()

    has_errors = len(errors) > 0
    if has_errors:
        logger.error("[BINANCE_FUT_API] close-all completed with %d errors: %s", len(errors), errors)

    return {"ok": not has_errors, "closed": closed, "errors": errors, "state": "DORMANT",
            "orphans_found": sum(1 for c in closed if c.get("orphan"))}


@router.post("/close-one")
def focus_close_one(
    request: Request,
    market: str = Query(..., description="Market to close (e.g. ZECUSDT)"),
):
    """Close a single FOCUS position by market symbol."""
    fm = _get_fm(request)
    import time as _time

    # Find the matching position
    target = None
    for p in fm.positions:
        if p.market.upper() == market.upper():
            target = p
            break

    if not target:
        return {"ok": False, "error": f"{market} not found in positions"}

    try:
        # ★ [2026-04-18] For profit/loss determination: capture current price before closing
        # A profitable manual exit is a "take-profit" → exempt from the 4-hour penalty (explicit user request)
        _cur_price_pre = None
        try:
            _cur_price_pre = fm._get_current_price(market)
        except Exception:
            pass
        _was_profit = False
        if _cur_price_pre and target.entry_price > 0:
            if target.direction == "LONG":
                _was_profit = _cur_price_pre > target.entry_price
            else:
                _was_profit = _cur_price_pre < target.entry_price

        success = fm._close_position(target, reason="manual_close_one", is_sl=False)
        if success:
            fm.positions = [p for p in fm.positions if p.market.upper() != market.upper()]
            if not fm.positions:
                from app.manager.focus_manager import FocusState
                fm.position = None
                fm.state = FocusState.DORMANT
                fm.selected_market = ""
            else:
                fm.position = fm.positions[0]
            # ★ Manual-exit penalty — config toggle + profit/loss determination
            _penalty_hours = 0.0
            _penalty_reason = ""
            if not getattr(fm.config, "manual_exit_penalty_enabled", True):
                _penalty_reason = "disabled_by_config"
                logger.info("[FOCUS] Manual exit for %s — penalty DISABLED by config", market)
            elif _was_profit:
                _penalty_reason = "profit_exit"
                logger.info("[FOCUS] Manual exit at PROFIT for %s — no penalty (take-profit)", market)
            else:
                _penalty_hours = float(getattr(fm.config, "manual_exit_penalty_hours", 4.0))
                fm.apply_manual_exit_penalty(market, hours=_penalty_hours)
                _penalty_reason = "loss_exit"
            return {"ok": True, "closed": market, "remaining": len(fm.positions),
                    "penalty_hours": _penalty_hours, "profit_exit": _was_profit,
                    "penalty_reason": _penalty_reason}
        else:
            # ★ Failure detail: retry directly against Bybit and capture the error
            detail = f"Close order failed for {market}"
            try:
                client = fm._get_client()
                side = "Sell" if target.direction == "LONG" else "Buy"
                client.place_order(market=target.market, side=side, ord_type="market", volume=target.qty, reduce_only=True)
                # Reaching here means it actually succeeded → remove the position
                fm.positions = [p for p in fm.positions if p.market.upper() != market.upper()]
                fm.position = fm.positions[0] if fm.positions else None
                if not fm.positions:
                    from app.manager.focus_manager import FocusState
                    fm.state = FocusState.DORMANT
                    fm.selected_market = ""
                fm._save_config()
                return {"ok": True, "closed": market, "remaining": len(fm.positions), "note": "retry_success"}
            except Exception as retry_exc:
                err_str = str(retry_exc)
                detail = f"{market}: {err_str}"
                # ★ Ghost detection: reduceOnly-related error or no position → auto-remove locally
                ghost_keywords = ["reduce only", "reduceonly", "position", "110017", "110043", "not enough", "qty not enough"]
                if any(kw in err_str.lower() for kw in ghost_keywords):
                    fm.positions = [p for p in fm.positions if p.market.upper() != market.upper()]
                    fm.position = fm.positions[0] if fm.positions else None
                    if not fm.positions:
                        from app.manager.focus_manager import FocusState
                        fm.state = FocusState.DORMANT
                        fm.selected_market = ""
                    fm._save_config()
                    logger.warning("[BINANCE_FUT_API] Ghost detected & removed: %s (%s)", market, err_str)
                    return {"ok": True, "closed": market, "remaining": len(fm.positions), "note": "ghost_removed"}
            return {"ok": False, "error": detail}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@router.post("/close-selected")
def focus_close_selected(
    request: Request,
    markets: str = Query(..., description="Comma-separated markets (e.g. ZECUSDT,DOGEUSDT)"),
):
    """Close multiple selected positions."""
    fm = _get_fm(request)
    market_list = [m.strip().upper() for m in markets.split(",") if m.strip()]
    closed = []
    errors = []

    for mkt in market_list:
        target = None
        for p in fm.positions:
            if p.market.upper() == mkt:
                target = p
                break
        if not target:
            errors.append({"market": mkt, "error": "not_found"})
            continue
        try:
            # ★ [2026-04-18] For profit/loss determination: capture current price before closing
            _cur_price_pre = None
            try:
                _cur_price_pre = fm._get_current_price(mkt)
            except Exception:
                pass
            _was_profit = False
            if _cur_price_pre and target.entry_price > 0:
                if target.direction == "LONG":
                    _was_profit = _cur_price_pre > target.entry_price
                else:
                    _was_profit = _cur_price_pre < target.entry_price

            success = fm._close_position(target, reason="manual_close_selected", is_sl=False)
            if success:
                fm.positions = [p for p in fm.positions if p.market.upper() != mkt]
                closed.append(mkt)
                # ★ Manual-exit penalty — config toggle + profit/loss determination
                if not getattr(fm.config, "manual_exit_penalty_enabled", True):
                    logger.info("[FOCUS] Manual exit for %s — penalty DISABLED by config", mkt)
                elif _was_profit:
                    logger.info("[FOCUS] Manual exit at PROFIT for %s — no penalty (take-profit)", mkt)
                else:
                    _hrs = float(getattr(fm.config, "manual_exit_penalty_hours", 4.0))
                    fm.apply_manual_exit_penalty(mkt, hours=_hrs)
            else:
                errors.append({"market": mkt, "error": "close_failed"})
        except Exception as exc:
            errors.append({"market": mkt, "error": str(exc)})

    # Clean up remaining positions
    if fm.positions:
        fm.position = fm.positions[0]
    else:
        fm.position = None
        from app.manager.focus_manager import FocusState
        fm.state = FocusState.DORMANT
        fm.selected_market = ""
    fm._save_config()

    return {"ok": len(errors) == 0, "closed": closed, "errors": errors, "remaining": len(fm.positions)}


@router.post("/restore-tp-sl")
def focus_restore_tp_sl(request: Request):
    """Re-apply TP/SL on Bybit after a restart (recover from evaporation)."""
    fm = _get_fm(request)
    if not fm.positions:
        return {"ok": True, "restored": 0, "message": "no positions"}
    results = []
    client = fm._get_client()
    for pos in fm.positions:
        tp = pos.tp2 if pos.partial_done else pos.tp1
        try:
            client.set_trading_stop(pos.market, take_profit=tp, stop_loss=pos.sl)
            results.append({"market": pos.market, "tp": round(tp, 6), "sl": round(pos.sl, 6), "status": "ok"})
        except Exception as exc:
            results.append({"market": pos.market, "status": "fail", "error": str(exc)})
    ok_count = sum(1 for r in results if r["status"] == "ok")
    return {"ok": ok_count == len(results), "restored": ok_count, "total": len(results), "details": results}


@router.post("/remove-ghost")
def focus_remove_ghost(
    request: Request,
    market: str = Query(..., description="Ghost position market to remove from local state"),
):
    """Remove a ghost position — clean up a position already closed on Bybit but still present locally."""
    fm = _get_fm(request)
    mkt = market.strip().upper()
    before = len(fm.positions)
    fm.positions = [p for p in fm.positions if p.market.upper() != mkt]
    removed = before - len(fm.positions)
    if removed > 0:
        fm.position = fm.positions[0] if fm.positions else None
        if not fm.positions:
            from app.manager.focus_manager import FocusState
            fm.state = FocusState.DORMANT
            fm.selected_market = ""
        fm._save_config()
        logger.info("[BINANCE_FUT_API] Ghost removed: %s (%d positions remaining)", mkt, len(fm.positions))
    return {"ok": removed > 0, "removed": mkt if removed > 0 else None, "remaining": len(fm.positions)}


@router.post("/amnesty")
def focus_amnesty(request: Request):
    """★ 2026-04-23 direct owner instruction: General Amnesty.

    "These were done while the rules were incomplete, so let's clear them for now."

    Clears all accumulated penalty records from the era of single B11 judgment:
    - _last_exit_* (re-entry blocking)
    - _manual_exit_penalties (manual-exit penalty)
    - only journal events after amnesty_ts are used for penalty calculation
      (affects B12 vote / direction_exhaustion / profit_exit_block, etc.)

    The journal records themselves are preserved (keeps the audit trail)."""
    fm = _get_fm(request)
    result = fm.execute_amnesty()
    logger.info("[BINANCE_FUT_API] ★ AMNESTY granted via API: %s", result)
    return result


@router.post("/clear-state")
def focus_clear_state(request: Request):
    """Clear local position state WITHOUT closing on Bybit (orphan cleanup)."""
    fm = _get_fm(request)
    count = len(fm.positions)
    fm.positions = []
    fm.position = None
    from app.manager.focus_manager import FocusState
    fm.state = FocusState.DORMANT
    fm.selected_market = ""
    fm.selected_direction = ""
    fm.daily_plans_used = 0
    fm.daily_sl_count = 0
    fm._save_config()
    logger.info("[BINANCE_FUT_API] State cleared: %d positions removed from tracking", count)
    return {"ok": True, "cleared": count, "state": "DORMANT"}


# ── Debug: Scanner candidate diagnostics ──────────────────────────────
@router.get("/debug/scanner")
def focus_debug_scanner(request: Request):
    """Return scanner candidates + each filter's result (for diagnostics)."""
    fm = _get_fm(request)
    import time as _t
    held = {p.market.upper() for p in fm.positions}
    if fm.config.lock_market:
        held.add(fm.config.lock_market.upper())
    try:
        candidates = fm._get_scanner_candidates()
    except Exception as exc:
        return {"ok": False, "error": f"_get_scanner_candidates failed: {exc}"}
    diagnostics = []
    for c in candidates:
        sym = c.get("market", "")
        d = {"market": sym, "signal": c.get("signal"), "adx": c.get("adx", 0),
             "pa": c.get("pa_pattern"), "price": c.get("price", 0), "blocks": []}
        if sym in held:
            d["blocks"].append("held")
        if sym in fm.config.scanner_blacklist:
            d["blocks"].append("blacklisted")
        if c.get("signal") not in ("BUY", "SELL"):
            d["blocks"].append(f"signal={c.get('signal')}")
        if c.get("adx", 0) < fm.config.scanner_min_adx:
            d["blocks"].append(f"adx<{fm.config.scanner_min_adx}")
        if c.get("pa_pattern", "-") == "-":
            d["blocks"].append("no_pa")
        if c.get("price", 0) > 0 and c.get("price", 0) < 5.0:
            d["blocks"].append(f"low_price(${c.get('price'):.2f})")
        direction = "LONG" if c.get("signal") == "BUY" else "SHORT"
        same_dir = sum(1 for p in fm.positions if p.direction == direction)
        if same_dir >= fm.config.max_same_direction:
            d["blocks"].append(f"dir_limit({direction}={same_dir})")
        if not d["blocks"]:
            d["blocks"].append("PASS ✓")
        diagnostics.append(d)
    return {"ok": True, "held": list(held), "total_candidates": len(candidates),
            "diagnostics": diagnostics}


# ── WhaleRadar ──────────────────────────────────────────────
@router.get("/whale")
def focus_whale_status(request: Request):
    """Current WhaleRadar state — active alerts + recent history."""
    try:
        from app.core.whale_radar import whale_radar
        return {
            "ok": True,
            "active_alerts": whale_radar.get_active_alerts(),
            "history_24h": whale_radar.get_history(hours=24),
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ── Config ──────────────────────────────────────────────────

@router.get("/config")
def focus_get_config(request: Request):
    """Get current FOCUS configuration."""
    fm = _get_fm(request)
    from dataclasses import asdict
    return {"ok": True, "config": asdict(fm.config)}


@router.get("/config/defaults")
def focus_get_config_defaults():
    """Get FOCUS configuration dataclass factory defaults (for resetting to defaults)."""
    from dataclasses import asdict
    from app.manager.focus_manager import FocusConfig
    return {"ok": True, "defaults": asdict(FocusConfig())}


# ────────────────────────────────────────────────────────────
# ★ [2026-05-19 owner decision] Automatic config snapshot system (history of 10)
# Owner's vision: "Once 10 settings exist, we can later check which setting maximized profit."
# Auto-save on every POST /config → runtime/config_snapshots/snapshot_YYYYMMDD_HHMMSS.json
# When over 10, the oldest is auto-deleted (FIFO).
# ────────────────────────────────────────────────────────────
_PNL24H_CACHE = {"t": 0.0, "v": {}}
_PNL24H_TTL = 60.0


def _calc_pnl_24h() -> dict:
    """24h EXIT stats from focus_harpoon_journal.jsonl.

    ★ [2026-06-20] TTL cache (60s) — avoids a full raw-journal parse on every call (110k lines, ~33s on a slow single-core server).
    The config-save snapshot calls this, which was the root cause of the save POST blocking for 33s (vs. the 2s drawdown reset).
    """
    import time as _t24
    if _PNL24H_CACHE["v"] and (_t24.time() - _PNL24H_CACHE["t"]) < _PNL24H_TTL:
        return _PNL24H_CACHE["v"]
    try:
        from pathlib import Path
        import json as _json
        from datetime import datetime, timedelta
        journal = Path(_BINANCE_FUT_JOURNAL_PATH)
        if not journal.exists():
            return {}
        cutoff = (datetime.now() - timedelta(hours=24)).timestamp()
        wins = losses = 0
        total = 0.0
        with open(journal, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    e = _json.loads(line)
                    if e.get("event") != "EXIT":
                        continue
                    if e.get("ts", 0) < cutoff:
                        continue
                    pnl = float(e.get("pnl_net", 0) or 0)
                    if pnl == 0:
                        continue
                    total += pnl
                    if pnl > 0:
                        wins += 1
                    else:
                        losses += 1
                except Exception:
                    pass
        result = {
            "trades": wins + losses,
            "wins": wins,
            "losses": losses,
            "total_pnl": round(total, 3),
            "win_rate": round(wins / (wins + losses) * 100, 1) if (wins + losses) > 0 else 0.0,
        }
        _PNL24H_CACHE["t"] = _t24.time()
        _PNL24H_CACHE["v"] = result
        return result
    except Exception:
        return {}


def _save_config_snapshot(config: dict, patch: dict) -> None:
    """Save a snapshot on config change. Maintain 10."""
    try:
        from pathlib import Path
        import json as _json
        from datetime import datetime
        import time
        snap_dir = Path(_BINANCE_FUT_CFGSNAP_DIR)
        snap_dir.mkdir(parents=True, exist_ok=True)
        ts = time.time()
        dt = datetime.fromtimestamp(ts)
        fname = f"snapshot_{dt.strftime('%Y%m%d_%H%M%S')}.json"
        fpath = snap_dir / fname
        # patch_diff = only the actually-changed items (excluding None, lists kept as-is)
        patch_diff = {k: v for k, v in patch.items() if v is not None}
        snapshot = {
            "ts": ts,
            "dt": dt.isoformat(),
            "patch_diff": patch_diff,
            "config": config,
            "pnl_24h": _calc_pnl_24h(),
        }
        with open(fpath, "w", encoding="utf-8") as f:
            _json.dump(snapshot, f, ensure_ascii=False, indent=2)
        # When over 10, delete the oldest (FIFO)
        snaps = sorted(snap_dir.glob("snapshot_*.json"))
        while len(snaps) > 10:
            try:
                snaps[0].unlink()
                snaps.pop(0)
            except Exception:
                break
    except Exception as exc:
        import logging
        logging.getLogger(__name__).debug(f"[FOCUS] snapshot save failed: {exc}")


@router.get("/config/snapshots")
def focus_config_snapshots():
    """List the 10 saved snapshots (timestamp desc).

    Returns: [{filename, ts, dt, patch_diff, pnl_24h}, ...]
    """
    try:
        from pathlib import Path
        import json as _json
        snap_dir = Path(_BINANCE_FUT_CFGSNAP_DIR)
        if not snap_dir.exists():
            return {"ok": True, "snapshots": []}
        items = []
        for fpath in sorted(snap_dir.glob("snapshot_*.json"), reverse=True)[:10]:
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    data = _json.load(f)
                items.append({
                    "filename": fpath.name,
                    "ts": data.get("ts"),
                    "dt": data.get("dt"),
                    "patch_diff": data.get("patch_diff", {}),
                    "pnl_24h": data.get("pnl_24h", {}),
                })
            except Exception:
                pass
        return {"ok": True, "snapshots": items}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@router.get("/config/snapshots/{filename}")
def focus_config_snapshot_single(filename: str):
    """Return the full config of a single snapshot (for restore)."""
    try:
        from pathlib import Path
        import json as _json
        snap_dir = Path(_BINANCE_FUT_CFGSNAP_DIR)
        # Security: validate filename (only snapshot_YYYYMMDD_HHMMSS.json allowed)
        if not filename.startswith("snapshot_") or not filename.endswith(".json"):
            return {"ok": False, "error": "invalid filename"}
        fpath = snap_dir / filename
        if not fpath.exists():
            return {"ok": False, "error": "not found"}
        with open(fpath, "r", encoding="utf-8") as f:
            data = _json.load(f)
        return {"ok": True, **data}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@router.post("/config")
def focus_set_config(
    request: Request,
    budget_usdt: Optional[float] = Query(None, ge=0, description="0=auto budget from system"),
    leverage: Optional[int] = Query(None, ge=1, le=100),
    max_positions: Optional[int] = Query(None, ge=1, le=99, description="Max concurrent position slots"),
    direction_mode: Optional[str] = Query(None),
    risk_pct: Optional[float] = Query(None, ge=1, le=50),
    max_daily_plans: Optional[int] = Query(None, ge=1, le=999),
    max_daily_sl: Optional[int] = Query(None, ge=1, le=999),
    cooldown_sec: Optional[float] = Query(None, ge=0),
    scan_interval_sec: Optional[float] = Query(None, ge=30),
    cycle_tp1_mult: Optional[float] = Query(None, ge=0.1, le=20),
    cycle_tp2_mult: Optional[float] = Query(None, ge=0.2, le=30),
    cycle_sl_mult: Optional[float] = Query(None, ge=0.1, le=100),
    partial_exit_pct: Optional[float] = Query(None, ge=10, le=90),
    trailing_pct: Optional[float] = Query(None, ge=0.1, le=10),
    # ── Dynamic Trailing SL ──
    dynamic_trailing: Optional[bool] = Query(None, description="Dynamic trailing SL ON/OFF"),
    breakeven_trigger_pct: Optional[float] = Query(None, ge=0.1, le=5.0, description="Break-even lock trigger (%)"),
    trailing_preserve_pct: Optional[float] = Query(None, ge=10, le=90, description="Peak-profit preservation rate-base (%)"),
    trailing_small_profit_preserve_pct: Optional[float] = Query(None, ge=10, le=95, description="Small-profit preservation rate (<0.5%) (%)"),
    trailing_accel_pct: Optional[float] = Query(None, ge=0, le=30, description="Preservation-rate acceleration per 1% profit (%)"),
    # ── v2: ADX / Conviction ──
    adx_filter_enabled: Optional[bool] = Query(None, description="ADX filter ON/OFF"),
    min_adx_entry: Optional[int] = Query(None, ge=10, le=50, description="Min ADX (entry threshold)"),
    dormant_adx_threshold: Optional[int] = Query(None, ge=5, le=30, description="DORMANT ADX threshold"),
    min_conviction: Optional[float] = Query(None, ge=0, le=100, description="[2026-05-17 100-scale] Min conviction score (0~100)"),
    # ── Scanner Multi-Slot ──
    scanner_entry: Optional[bool] = Query(None, description="Scanner multi-slot ON/OFF"),
    scanner_min_adx: Optional[int] = Query(None, ge=15, le=50),
    scanner_min_conviction: Optional[float] = Query(None, ge=0, le=100),
    scanner_max_exposure_pct: Optional[float] = Query(None, ge=10, le=100),
    scanner_m30_primary_conflict_penalty: Optional[float] = Query(None, ge=0.1, le=1.0, description="Conviction multiplier when PRI(H1) vs 30M trend conflict (default 0.7 = -30%, 1.0=OFF, 0.5=old value)"),
    scanner_m30_direction_conflict_penalty: Optional[float] = Query(None, ge=0.1, le=1.0, description="Conviction multiplier when direction(LONG/SHORT) vs 30M trend conflict (default 0.7 = -30%, 1.0=OFF, 0.5=old value)"),
    # ── ★ [2026-05-20 Phase 6 redesign] Entry Mode (Score vs Reverse) ──
    entry_mode: Optional[str] = Query(None, regex="^(score|reverse)$", description="Entry mode: score (conviction 100-scale, default) / reverse (bot signal + low conv + low ADX → auto opposite entry, operator primary rule)"),
    # ── ★★★ [2026-05-28 evening owner decision] Guard-bundle master toggle ★★★ ──
    # Cleanup of 9 months of accumulated guards — owner's classification: precise entry + no loss + max profit = only 2 kinds.
    # 🟢 green = Phase 6/7 (D1+H4+H1+30M+15M+5M + PA + 5-condition rule) — aggressive entry
    # 🟡 yellow = old strict guards (BE Stall / Pre-BE / Reverse Drift / Entry Quality Gates etc.)
    # both = both ON (strictest, not recommended)
    # minimal = core only (TF+PA+SL/HardROE — "endure to the death" mode)
    entry_guard_set: Optional[str] = Query(None, regex="^(green|yellow|both|minimal)$", description="🟢 green (aggressive, Phase 6/7) / 🟡 yellow (cautious, old strict guards) / both / minimal. Switch the entire entry-guard bundle with one click. default=green."),
    exit_guard_set: Optional[str] = Query(None, regex="^(green|yellow|both|minimal)$", description="🟢 green (charge_exit/tight_trail/exit_5m etc.) / 🟡 yellow (old 11 EXIT guards: BE Stall/Pre-BE/Reverse Drift etc.) / both / minimal (SL/HardROE only — endure to the end). default=green."),
    smart_manual_entry_enabled: Optional[bool] = Query(None, description="[2026-05-29 operator] Smart Manual Entry (enter after signal confirmation) ON/OFF. When OFF, L⏳/S⏳ buttons don't work (immediate-entry L/S only)."),
    smart_manual_entry_default_timeout_sec: Optional[float] = Query(None, ge=60, le=86400, description="Smart Manual Entry wait time (sec). UI takes minutes → ×60. default 3600 (1 hour)."),
    slot_auto_expand_enabled: Optional[bool] = Query(None, description="[2026-05-29 operator] Slot Auto Expand (temporary +1 slot on strong signal) ON/OFF. Redesigned from the old simple time-based one (rolled back) → strong-signal + locked + limited + capital-protected."),
    slot_auto_expand_lock_hours: Optional[float] = Query(None, ge=0.1, le=24, description="Condition: all slots locked for N hours (default 1.0)"),
    slot_auto_expand_min_conviction: Optional[float] = Query(None, ge=50, le=100, description="Strong-signal min conv (default 85)"),
    slot_auto_expand_max_extra: Optional[int] = Query(None, ge=1, le=3, description="Max extra slots (default 1, no unlimited expansion)"),
    slot_auto_expand_size_ratio: Optional[float] = Query(None, ge=0.1, le=1.0, description="N% of the average size (default 0.5 = 50%, capital protection)"),
    market_consensus_exit_enabled: Optional[bool] = Query(None, description="[2026-05-29 operator] Market Consensus Exit (close losing positions when the market consensus goes the opposite way) ON/OFF. Handles the asymmetry: 'profit seesaws / loss is a slide'."),
    market_consensus_threshold_pct: Optional[float] = Query(None, ge=50, le=100, description="One-direction signal ratio threshold (default 70%)"),
    market_consensus_duration_min: Optional[float] = Query(None, ge=1, le=60, description="Consensus duration (default 15 min)"),
    market_consensus_min_hold_min: Optional[float] = Query(None, ge=5, le=240, description="Min position hold (protects right after entry, default 20 min)"),
    market_consensus_min_pnl_pct: Optional[float] = Query(None, ge=-10, le=0, description="Close only when PnL ≤ N% (protects in-profit reversals, default -0.5%)"),
    reverse_conv_threshold: Optional[float] = Query(None, ge=20, le=70, description="Reverse-mode conv threshold. Opposite entry when at/below this + ADX condition met. default 50 (40~55 recommended)"),
    reverse_adx_max: Optional[float] = Query(None, ge=5, le=40, description="Reverse-mode ADX threshold. Opposite entry when at/below this (= choppy spot) + conv condition met. default 20 (15~25 recommended)"),
    # ── ★ [2026-05-21 Phase 6 Stage 8 re-redesign] Score-recovery auto exit ──
    charge_exit_enabled: Optional[bool] = Query(None, description="Score-recovery auto exit ON/OFF. Operator rule: 'exit only in profit, and if the score shows signs of recovering even if it can't reach BE'. Small exit when in profit + conv recovers. Existing guards (BE/SL) unchanged."),
    charge_exit_min_pnl_pct: Optional[float] = Query(None, ge=-5, le=5, description="Score-recovery exit profit condition (%). Triggers only when pnl ≥ this. default 0 = pnl>0 (in profit). Operator: 'exit only in profit'"),
    charge_exit_conv_delta: Optional[float] = Query(None, ge=1, le=30, description="Score-recovery exit conv threshold. Exit when conv rises this much above entry conv. default 5 (3=sensitive, 10=conservative). Operator: 'signs of score recovery'"),
    max_same_direction: Optional[int] = Query(None, ge=1, le=15, description="Max positions in the same direction"),
    regime_direction_lock_freeze_sec: Optional[float] = Query(None, ge=300, le=86400, description="Freeze time after a regime change (sec). 30min=1800, 1h=3600, 4h=14400"),
    regime_direction_lock_neutral_block: Optional[bool] = Query(None, description="Block both directions on NEUTRAL regime (REST). true=rest, false=allow both directions"),
    # ── Coin Loss Cap ──
    coin_loss_cap_enabled: Optional[bool] = Query(None, description="Per-coin 24h loss cap ON/OFF"),
    coin_loss_cap_amount: Optional[float] = Query(None, ge=5, le=500, description="Max loss per coin ($)"),
    coin_loss_cap_window_hours: Optional[float] = Query(None, ge=1, le=72, description="Loss aggregation window (hours)"),
    # ── Per-Coin Size Cap (★ 2026-05-08 owner decision) ──
    per_coin_size_cap_enabled: Optional[bool] = Query(None, description="Cap single-coin size at a % of capital ON/OFF"),
    per_coin_size_cap_pct: Optional[float] = Query(None, ge=1, le=100, description="Max single-coin size as % of capital"),
    # ── Conviction Override Slot (★ 2026-05-10 owner decision) ──
    override_slot_enabled: Optional[bool] = Query(None, description="Override slot ON/OFF (extra entries equal to the number of slots locked for ≥ window(h))"),
    override_min_conviction: Optional[float] = Query(None, ge=0, le=100, description="[100-scale] Override-slot entry min conviction (default 75)"),
    override_locked_slot_min_hours: Optional[float] = Query(None, ge=1, le=720, description="★ Locked-slot recognition window(h) — counts only slots held longer than this (default 24)"),
    override_size_cap_pct: Optional[float] = Query(None, ge=1, le=50, description="Override-slot size cap (% of capital, default 8)"),
    override_max_sl_distance_pct: Optional[float] = Query(None, ge=1, le=50, description="Override-slot max SL distance % (default 5)"),
    override_hard_roe_cut_pct: Optional[float] = Query(None, ge=-100, le=0, description="Override-slot Hard ROE instant-cut % (default -10)"),
    # ── Momentum Reversal (hard penalty 18 from Phase 4) ──
    momentum_reversal_enabled: Optional[bool] = Query(None, description="Momentum-reversal penalty ON/OFF (5m 1~3 bars reversing right before entry)"),
    momentum_reversal_strong_atr: Optional[float] = Query(None, ge=0.1, le=5.0, description="Strong reversal ATR threshold (default 1.0)"),
    momentum_reversal_medium_atr: Optional[float] = Query(None, ge=0.1, le=5.0, description="Medium reversal ATR threshold (default 0.5)"),
    momentum_reversal_strong_weight: Optional[float] = Query(None, ge=-100, le=0, description="[100-scale ×10] Strong-reversal penalty (default -30)"),
    momentum_reversal_medium_weight: Optional[float] = Query(None, ge=-100, le=0, description="[100-scale ×10] Medium-reversal penalty (default -20)"),
    momentum_reversal_lookback_bars: Optional[int] = Query(None, ge=1, le=5, description="5m cumulative lookback bars (default 3)"),
    # ── Coin Repeat Brake ──
    coin_repeat_brake_enabled: Optional[bool] = Query(None, description="Coin repeat-entry brake ON/OFF"),
    coin_repeat_free_count: Optional[int] = Query(None, ge=0, le=20, description="Free entry count (0 = apply cooldown from the first entry)"),
    coin_repeat_cooldown_base: Optional[float] = Query(None, ge=60, le=3600, description="Cooldown base seconds"),
    # ── ★ BE Stall Exit (2026-05-14 owner — exposed in UI) ──
    be_stall_exit_enabled: Optional[bool] = Query(None, description="BE Stall Exit: close when stalling after BE ON/OFF"),
    be_stall_exit_sec: Optional[float] = Query(None, ge=5.0, le=300.0, description="BE Stall Exit: stall time after BE in seconds (default 30)"),
    be_stall_intelligent_enabled: Optional[bool] = Query(None, description="BE Stall intelligent: tied to momentum (MACD/RSI/BB 5m) — in our favor HOLD / against us close immediately / neutral fallback"),
    be_stall_intelligent_rsi_strong: Optional[float] = Query(None, ge=50.0, le=80.0, description="Intelligent RSI strong threshold (LONG: ≥ this = in our favor / default 55)"),
    be_stall_intelligent_rsi_weak: Optional[float] = Query(None, ge=20.0, le=50.0, description="Intelligent RSI weak threshold (LONG: ≤ this = against us / default 45)"),
    # ── ★ Pre-BE Stall Exit (2026-04-23 owner direct request) ──
    pre_be_stall_exit_mode: Optional[str] = Query(None, description="Pre-BE Stall: AUTO (follow market) / ON / OFF"),
    pre_be_stall_min_profit_pct: Optional[float] = Query(None, ge=0.01, le=2.0, description="Pre-BE Stall: min profit % (default 0.10)"),
    pre_be_stall_sec: Optional[float] = Query(None, ge=10.0, le=600.0, description="Pre-BE Stall: stall time in seconds (default 60)"),
    pre_be_stall_volatility_threshold_pct: Optional[float] = Query(None, ge=0.5, le=10.0, description="Pre-BE Stall AUTO threshold ATR% (default 2.0, below=ranging=ON)"),
    pre_be_stall_max_since_peak_sec: Optional[float] = Query(None, ge=300.0, le=86400.0, description="Pre-BE Stall: max time after peak (default 1800=30min, beyond=stale → not triggered)"),
    # ── 🐢 Pre-BE loss guard (2026-06-09 owner "right now") ──
    pre_be_loss_guard_enabled: Optional[bool] = Query(None, description="🐢 Pre-BE loss guard: small cut when a peak<0.1 dither slips into an entry loss (default OFF)"),
    pre_be_loss_guard_peak_max_pct: Optional[float] = Query(None, ge=0.0, le=1.0, description="peak ≤ this = dither target (default 0.10)"),
    pre_be_loss_guard_trigger_loss_pct: Optional[float] = Query(None, ge=0.1, le=3.0, description="Cut when it slips -this % below entry (default 0.5, half of SL)"),
    pre_be_loss_guard_min_hold_sec: Optional[float] = Query(None, ge=0.0, le=3600.0, description="Min hold seconds after entry (default 60)"),
    pre_be_loss_guard_max_age_sec: Optional[float] = Query(None, ge=60.0, le=86400.0, description="Stale protection — not triggered beyond this time (default 7200=2h)"),
    # ── ★ Reverse Drift Exit (2026-05-16 owner direct request) ──
    reverse_drift_exit_enabled: Optional[bool] = Query(None, description="Reverse Drift Exit: cut when drifting back from peak (complements pre_be_stall, triggers regardless of ATR)"),
    reverse_drift_peak_min_pct: Optional[float] = Query(None, ge=0.01, le=1.0, description="Trigger peak min % (default 0.10)"),
    reverse_drift_peak_max_pct: Optional[float] = Query(None, ge=0.05, le=2.0, description="Trigger peak max % (default 0.35 = below BE_trigger 0.4, 0.05 gap)"),
    reverse_drift_min_since_peak_sec: Optional[float] = Query(None, ge=30.0, le=1800.0, description="Min stall time after peak in seconds (default 180)"),
    reverse_drift_max_since_peak_sec: Optional[float] = Query(None, ge=300.0, le=86400.0, description="Max time after peak in seconds (default 1800=30min, beyond this=stale → not triggered)"),
    reverse_drift_pct: Optional[float] = Query(None, ge=0.01, le=1.0, description="Reversal threshold % (ATR adaptation OFF or floor, default 0.26)"),
    reverse_drift_atr_adaptive_enabled: Optional[bool] = Query(None, description="ATR-based adaptive threshold ON/OFF (default ON)"),
    reverse_drift_atr_multiplier: Optional[float] = Query(None, ge=0.05, le=1.0, description="atr_pct × multiplier = threshold (default 0.2)"),
    reverse_drift_atr_cap_pct: Optional[float] = Query(None, ge=0.1, le=2.0, description="Adaptive threshold cap (default 0.4)"),
    # ── ★ Late-chase block (Overextension) — 2026-06-07 owner (live ON) ──
    overextension_enabled: Optional[bool] = Query(None, description="Late-chase penalty ON/OFF: top of 24H range (LONG)/bottom (SHORT) + large move = chasing an exhausted trend → conviction penalty (default ON)"),
    overextension_range_pos_pct: Optional[float] = Query(None, ge=0.5, le=1.0, description="LONG trigger position in 24H range (default 0.85 = top 15%). SHORT uses 1-this"),
    overextension_min_move_pct: Optional[float] = Query(None, ge=0.0, le=50.0, description="Trigger min 24H move |%| (default 8.0, excludes small moves)"),
    overextension_penalty: Optional[float] = Query(None, ge=0.0, le=50.0, description="Conviction penalty points (default 10)"),
    overextension_adx_exempt: Optional[float] = Query(None, ge=0.0, le=100.0, description="ADX ≥ this = strong breakout → penalty exempt (default 30, 0=no exemption)"),
    blowoff_filter_enabled: Optional[bool] = Query(None, description="[#1 late filter] Block chasing 24h blow-off spikes/dumps ON/OFF (default OFF, no ADX exemption)"),
    blowoff_move_pct: Optional[float] = Query(None, ge=5, le=300, description="[#1] 24h |move| ≥ this %=blow-off candidate (default 30)"),
    blowoff_penalty: Optional[float] = Query(None, ge=0, le=100, description="[#1] Base penalty (default 20)"),
    blowoff_extreme_pct: Optional[float] = Query(None, ge=10, le=500, description="[#1] Extreme move % (max penalty, default 80)"),
    blowoff_max_penalty: Optional[float] = Query(None, ge=0, le=150, description="[#1] Extreme max penalty (default 40)"),
    blowoff_chase_only: Optional[bool] = Query(None, description="[#1] True=penalize chase (same direction) only, fade exempt (default True)"),
    # 🎯 Inflection setup score — 2026-06-12 owner "the score betrays the chart"
    inflection_setup_enabled: Optional[bool] = Query(None, description="Inflection setup score ON/OFF: position (move)×momentum → top-stall LONG penalty/SHORT bonus, bottom-inflection LONG bonus, wall-riding exempt (default OFF)"),
    inflection_setup_weight: Optional[float] = Query(None, ge=0.0, le=60.0, description="Inflection modifier max magnitude W (default 20)"),
    inflection_setup_cap: Optional[float] = Query(None, ge=0.0, le=60.0, description="Output clamp ±cap (default 20)"),
    inflection_setup_base: Optional[float] = Query(None, ge=0.0, le=1.0, description="Base adjustment from position alone (default 0.45)"),
    inflection_setup_slope_scale: Optional[float] = Query(None, ge=0.05, le=5.0, description="slope15m tanh normalization scale % (default 0.40)"),
    # 🎣 Retest setup score (2026-06-12 owner/sibling) — breakout→pullback→support = good entry spot
    retest_setup_enabled: Optional[bool] = Query(None, description="Retest bonus ON/OFF: breakout→pullback→support+turning = good entry spot bonus (default OFF)"),
    retest_setup_weight: Optional[float] = Query(None, ge=0.0, le=40.0, description="Retest bonus max magnitude (default 12)"),
    retest_setup_turn_bonus: Optional[float] = Query(None, ge=0.0, le=20.0, description="Extra bonus on turning after pullback (default 4)"),
    retest_retr_lo: Optional[float] = Query(None, ge=0.0, le=1.0, description="Min pullback ratio, below=not a top-chase signal (default 0.30)"),
    retest_retr_hi: Optional[float] = Query(None, ge=0.0, le=1.5, description="Ideal pullback upper bound, +0.3 over=too-deep (default 0.90)"),
    # 🌋 Volatility-awakening SL adaptation (2026-06-11 owner "set it far and trail") — widening SL auto-shrinks size↓ to fix risk
    awaken_sl_enabled: Optional[bool] = Query(None, description="Volatility-awakening SL adaptation: on awakening + Day-aligned, widen SL + auto-shrink size (fixed risk) (default OFF)"),
    awaken_sl_mode: Optional[str] = Query(None, description="SL distance basis: atr / structure / both (default both=the farther one)"),
    awaken_atr_ratio: Optional[float] = Query(None, ge=1.0, le=5.0, description="Awakening判定 current/past ATR ratio (default 1.3)"),
    awaken_atr_lookback: Optional[int] = Query(None, ge=10, le=100, description="Past ATR average bar count H4 (default 20)"),
    awaken_max_sl_mult: Optional[float] = Query(None, ge=1.0, le=5.0, description="SL max multiplier to prevent unlimited expansion (default 2.5)"),
    awaken_require_day_align: Optional[bool] = Query(None, description="Only Day (coin D1) alignment qualifies to endure (default True, excludes against/undecided)"),
    awaken_swing_lookback: Optional[int] = Query(None, ge=3, le=50, description="Structure-point (awakening foot) swing search bars (default 10)"),
    awaken_atr_buffer: Optional[float] = Query(None, ge=0.0, le=3.0, description="ATR buffer multiplier on the structure point (default 0.5)"),
    # ② Late ceiling penalty (2026-06-09 owner "90+=late=50↓, wall-riding exception")
    conviction_ceiling_enabled: Optional[bool] = Query(None, description="② Late ceiling penalty: cap conviction 90+ down to target (default OFF)"),
    conviction_ceiling_start: Optional[float] = Query(None, ge=50.0, le=150.0, description="conviction ≥ this = late candidate (default 90)"),
    conviction_ceiling_target: Optional[float] = Query(None, ge=0.0, le=100.0, description="Cap late entries to this score (default 50, below 65=blocked)"),
    conviction_ceiling_adx_exempt: Optional[float] = Query(None, ge=0.0, le=100.0, description="ADX ≥ this = wall-riding exempt (default 30, 0=no exemption)"),
    # ★ Headroom penalty (2026-06-09 owner "penalize even if the direction is right but there's nowhere to go")
    headroom_penalty_enabled: Optional[bool] = Query(None, description="Headroom penalty: penalize entries right at resistance/support, RSI extremes, BB band edge (default OFF)"),
    headroom_sr_penalty: Optional[float] = Query(None, ge=0.0, le=30.0, description="LONG right at resistance / SHORT right at support penalty (default 6)"),
    headroom_sr_near_pct: Optional[float] = Query(None, ge=0.1, le=10.0, description="Within this % of resistance/support = no headroom (default 1.5)"),
    headroom_rsi_penalty: Optional[float] = Query(None, ge=0.0, le=30.0, description="LONG overbought / SHORT oversold penalty (default 6)"),
    headroom_rsi_overbought: Optional[float] = Query(None, ge=50.0, le=100.0, description="LONG: RSI ≥ this = nowhere to go (default 70)"),
    headroom_rsi_oversold: Optional[float] = Query(None, ge=0.0, le=50.0, description="SHORT: RSI ≤ this = nowhere to go (default 30)"),
    headroom_bb_penalty: Optional[float] = Query(None, ge=0.0, le=30.0, description="LONG BB upper / SHORT BB lower penalty (default 4)"),
    headroom_bb_hi_pctb: Optional[float] = Query(None, ge=0.5, le=1.5, description="%b ≥ this = band upper (default 0.80)"),
    headroom_bb_lo_pctb: Optional[float] = Query(None, ge=-0.5, le=0.5, description="%b ≤ this = band lower (default 0.20)"),
    # ── 🌊 Macro-down active SHORT entry, stage 2 (Macro Short Timing) — 2026-06-11 owner "complete the waterway" ──
    macro_short_timing_enabled: Optional[bool] = Query(None, description="Macro-down 2-stage: macro RISK_OFF + 5m bounce rollover = active SHORT entry (bonus). SHORT-only, late-chase prevention (default OFF)"),
    macro_short_timing_delta: Optional[float] = Query(None, ge=0.0, le=40.0, description="SHORT conviction bonus magnitude (default 12)"),
    macro_short_timing_min_signals: Optional[int] = Query(None, ge=1, le=3, description="Min of 3 rollover signals (turn negative/MACD<0/PA) to meet (default 2)"),
    macro_short_timing_bounce_pct: Optional[float] = Query(None, ge=0.0, le=5.0, description="Premise of 'bounce exists' — min % bounce to a high after a 5m low (default 0.3)"),
    macro_short_timing_lookback: Optional[int] = Query(None, ge=6, le=40, description="5m bounce search bars (default 12)"),
    # ── ★ Regime-counter holding exit P3 (2026-06-06 owner) — fix for missing router wiring (2026-06-07) ──
    macro_exit_enabled: Optional[bool] = Query(None, description="Regime-counter holding exit P3: move RISK_ON+SHORT / RISK_OFF+LONG holdings to a near SL exit (default OFF, exit guard)"),
    macro_exit_breadth_min: Optional[int] = Query(None, ge=5, le=10, description="Trigger breadth STRONG N/10 (default 8 = only when certain)"),
    macro_exit_sl_cushion_pct: Optional[float] = Query(None, ge=0.05, le=2.0, description="SL distance from current price % (default 0.15, smaller=quicker exit)"),
    macro_exit_strong_coin_exempt: Optional[bool] = Query(None, description="Individual-strength exception: hold even against the macro if in profit (default ON)"),
    macro_exit_exempt_min_roe: Optional[float] = Query(None, ge=0.0, le=20.0, description="Exception min price ROE% (default 0 = always exempt if in profit)"),
    # ── ★ Batch fix for missing router wiring (2026-06-07) — 12 fields that had dataclass+UI but were missing from POST (BB-wall-ride/regime-compass-P2/final5m/micro1m/multiBE) ──
    bb_block_trend_bypass_adx: Optional[float] = Query(None, ge=0.0, le=100.0, description="BB wall-riding: ADX ≥ this = strong trend bypasses BB-extreme block (0=disabled)"),
    bb_trend_bypass_require_di: Optional[bool] = Query(None, description="BB wall-riding ② require direction confirmation (DI)"),
    bb_trend_bypass_macd_min: Optional[float] = Query(None, ge=0.0, le=10.0, description="BB wall-riding ③ MACD momentum tolerance (0=disabled)"),
    final_30m15m_bypass_conviction: Optional[float] = Query(None, ge=0, le=200, description="final_30m15m score-absorb — if conviction ≥ this, block is exempt (0=OFF, e.g. 75)"),
    final_30m15m_bypass_include_regime: Optional[bool] = Query(None, description="Include macro-counter (regime_opposed) in score-absorb too (True=include / False=exclude, existing)"),
    final_d1_bypass_conviction: Optional[float] = Query(None, ge=0, le=200, description="D1 score-absorb — if conviction ≥ this, the D1-counter block is exempt (0=OFF, e.g. 78). Backed by exit guards"),
    final_d1_recent5_override_enabled: Optional[bool] = Query(None, description="final_d1 recent-5-bar override — when D1=UPTREND is a misread (lookback=5 afterimage), if the last 5 daily bars are clearly DOWN, let SHORT pass (default OFF)"),
    final_d1_recent5_drop_pct: Optional[float] = Query(None, ge=0, le=50, description="If the last 5 daily bars' change ≤ -this(%), ignore the UPTREND label and let SHORT pass (e.g. 1.0)"),
    d1_reality_demote_enabled: Optional[bool] = Query(None, description="Fix D — D1 trend label reality check: if labeled UPTREND but the last 5 daily bars ≤ -drop%, demote to SIDEWAYS (remove trend-alignment LONG credit). Blocks falling-knife LONGs, corrects the card label. default OFF"),
    d1_reality_demote_drop_pct: Optional[float] = Query(None, ge=0, le=50, description="If the last 5 daily bars' change ≤ -this(%), demote UPTREND→SIDEWAYS (e.g. 1.0)"),
    guard_score_total_cap_enabled: Optional[bool] = Query(None, description="[patch v1] Cap the total of guard bonus points ON/OFF (default OFF)"),
    guard_score_total_cap: Optional[float] = Query(None, ge=5, le=100, description="[patch v1] Total clamp ±N (default 30)"),
    conviction_ceiling_post_guards: Optional[bool] = Query(None, description="[patch v1] Apply the late ceiling after summing base+guards (default OFF)"),
    final_bypass_use_base: Optional[bool] = Query(None, description="[patch v1] Base the score-absorb bypass on base conviction (default OFF)"),
    final_5m_simple_check_enabled: Optional[bool] = Query(None, description="Check 5M RSI/MACD/BB agreement right before entry"),
    final_5m_simple_min_score: Optional[int] = Query(None, ge=0, le=3, description="Pass when ≥ N of the 3 5M signals agree"),
    final_5m_bb_trend_bypass_enabled: Optional[bool] = Query(None, description="final_5m BB wall-riding exempt — with a strong trend (ADX+DI), pass even at a BB extreme (SHORT bottom/LONG top). default OFF"),
    final_d1_alignment_check_enabled: Optional[bool] = Query(None, description="Require D1 alignment — block entry when the Day candle is the opposite direction (OFF=ignore Day candles shaken by events, 2026-06-07 operator)"),
    final_align_regime_override_enabled: Optional[bool] = Query(None, description="Macro-alignment override — on a clear crash (RISK_OFF), the final gate follows the macro direction instead of the higher TF (SHORT-aligned passes / LONG falling-knife blocked, 2026-06-07)"),
    macro_compass_enabled: Optional[bool] = Query(None, description="Regime compass P2 (RECOVERING bonus, default OFF paper)"),
    macro_recovering_conv_delta: Optional[float] = Query(None, ge=-50.0, le=50.0, description="RECOVERING LONG bonus / SHORT penalty magnitude (0=paper)"),
    macro_recovering_require_di_adx: Optional[bool] = Query(None, description="Dead-cat defense: bonus only with a +DI flip + ADX together"),
    macro_recovering_min_adx: Optional[float] = Query(None, ge=0.0, le=100.0, description="Min ADX to confirm recovery"),
    micro_1m_body_min_pct: Optional[float] = Query(None, ge=0.0, le=5.0, description="Filter weak 1M dojis — min body % for a real pushing bar"),
    multi_be_lock_atr_adaptive_enabled: Optional[bool] = Query(None, description="Multi BE-lock ATR-multiplier mode ON/OFF"),
    multi_be_lock_atr_min_stage1_trigger_pct: Optional[float] = Query(None, ge=0.0, le=5.0, description="Multi BE-lock stage1 trigger floor %"),
    multi_be_lock_atr_max_stage1_trigger_pct: Optional[float] = Query(None, ge=0.0, le=20.0, description="[2026-06-13] Multi BE-lock stage1 trigger cap % — even extreme ATR locks BE at +N% (0=no cap, default 3.0)"),
    # ── ★ Entry Grace Period (2026-05-18 owner vision #6) — time to read the mood after entry ──
    entry_grace_period_sec: Optional[float] = Query(None, ge=0.0, le=3600.0, description="Disable pre_be_stall + reverse_drift guards for N seconds after entry (buys time to tell A/B apart). 0=OFF, 300=5min recommended. Doesn't affect be_stall/SL/long_hold."),
    market_bias_grace_exit_enabled: Optional[bool] = Query(None, description="[operator vision #6 aux] During the grace period, force exit immediately when an opposite market_bias dominance is detected (avoid pattern A). default OFF. Must be on together with entry_grace_period_sec to trigger."),
    news_grace_exit_enabled: Optional[bool] = Query(None, description="[operator vision #6 aux — news revival] Force exit during the grace period when news sentiment is strongly opposite. default OFF. news_sentiment.focus_enabled (/api/news-sentiment/config) must also be on to trigger."),
    news_grace_exit_threshold: Optional[float] = Query(None, ge=0.1, le=1.0, description="[operator vision #6 aux] news_grace_exit trigger threshold |sentiment| (default 0.5)"),
    # ── ★★★★ [2026-05-18 owner vision #6 option B] Time-independent OR condition ──
    exit_consensus_enabled: Optional[bool] = Query(None, description="[operator vision #6 option B] Time-independent OR condition. When reverse_drift/pre_be_stall fires, combine the peer's + news's opinion. Same direction=hold (endure) / opposite=exit (follow the guard). Works without a time grace. default OFF."),
    exit_consensus_news_threshold: Optional[float] = Query(None, ge=0.1, le=1.0, description="[operator vision #6 option B] exit_consensus news sentiment strength threshold (default 0.3, mild)"),
    # ── Long Hold Timeout (3-tier, 2026-04-25) ──
    long_hold_timeout_enabled: Optional[bool] = Query(None, description="Long Hold Timeout (3-tier) ON/OFF"),
    long_hold_timeout_tier1_min: Optional[float] = Query(None, ge=0, le=99999, description="Tier1: time (min) — 0=disabled, 9999=effectively OFF"),
    long_hold_timeout_tier1_peak_pct: Optional[float] = Query(None, ge=0.01, le=2.0, description="Tier1: cut when peak < threshold(%)"),
    long_hold_timeout_tier2_min: Optional[float] = Query(None, ge=0, le=99999, description="Tier2: time (min) — 0=disabled, 9999=effectively OFF"),
    long_hold_timeout_tier2_peak_pct: Optional[float] = Query(None, ge=0.01, le=2.0, description="Tier2: cut when peak < threshold(%)"),
    long_hold_timeout_tier3_min: Optional[float] = Query(None, ge=0, le=99999, description="Tier3: time (min) — BE-distant cut (default 30, 9999=OFF)"),
    long_hold_timeout_tier3_peak_pct: Optional[float] = Query(None, ge=0.01, le=2.0, description="Tier3: cut when peak < threshold(%) (default 0.2)"),
    # ── ★ Entry Expectation (2026-05-14 owner — entry-expectation mechanism) ──
    entry_expectation_enabled: Optional[bool] = Query(None, description="On entry, compute reward/risk from primary_tf(H1) structure + unify exchange TP/SL ON/OFF"),
    expectation_progress_exit_enabled: Optional[bool] = Query(None, description="Progress-based exit (replaces LHT time cut) ON/OFF"),
    expectation_progress_t1_min: Optional[float] = Query(None, ge=1, le=600, description="Progress cut T1: N minutes elapsed (default 15)"),
    expectation_progress_t1_pct: Optional[float] = Query(None, ge=0, le=100, description="Progress cut T1: cut if target progress < M% (default 30)"),
    expectation_progress_t2_min: Optional[float] = Query(None, ge=1, le=600, description="Progress cut T2: N minutes elapsed (default 30)"),
    expectation_progress_t2_pct: Optional[float] = Query(None, ge=0, le=100, description="Progress cut T2: cut if target progress < M% (default 50)"),
    # ── ★ Negative-progress instant cut (2026-05-15 owner) ──
    expectation_progress_neg_cut_enabled: Optional[bool] = Query(None, description="Negative-progress instant cut (quick cut when the loss direction is clear)"),
    expectation_progress_neg_cut_pct: Optional[float] = Query(None, ge=-1000.0, le=0.0, description="Progress threshold (negative, default -50 = 50% progress opposite the target)"),
    expectation_progress_neg_cut_min: Optional[float] = Query(None, ge=1, le=600, description="Negative-cut min hold time (min, default 30)"),
    # ── ★ Entry Quality Gates (2026-05-15 owner) ──
    entry_expectation_gate_enabled: Optional[bool] = Query(None, description="#1 RR/risk gate: block entries below threshold ON/OFF (requires entry_expectation_enabled)"),
    entry_expectation_min_rr: Optional[float] = Query(None, ge=0, le=10, description="RR floor — block if below this (default 1.0, relaxed for operations)"),
    entry_expectation_min_reward_pct: Optional[float] = Query(None, ge=0, le=10, description="reward_pct floor — block if expected reach % is below this (default 0.8, spec Gate 2 of the 5-14 blueprint)"),
    entry_expectation_max_risk_pct: Optional[float] = Query(None, ge=0.5, le=30, description="risk_pct cap — block if over this (%) (default 6.0, safety net for the 5/15 SIREN incident)"),
    # ── 🌍 [2026-06-02 macro regime direction gate] Market Breadth (top-10 tsunami) ──
    breadth_strong_n: Optional[int] = Query(None, ge=1, le=10, description="STRONG threshold N/10 (default 8). N coins in unison = a strong tsunami"),
    breadth_mid_n: Optional[int] = Query(None, ge=1, le=10, description="MID threshold N/10 (default 6)"),
    breadth_aligned_strong: Optional[float] = Query(None, ge=0, le=100, description="Aligned STRONG bonus (default 12, following the flow=opportunity)"),
    breadth_aligned_mid: Optional[float] = Query(None, ge=0, le=100, description="Aligned MID bonus (default 6)"),
    breadth_counter_strong: Optional[float] = Query(None, ge=-100, le=0, description="Counter STRONG penalty (default -25, falling knife=blocked)"),
    breadth_counter_mid: Optional[float] = Query(None, ge=-100, le=0, description="Counter MID penalty (default -7)"),
    regime_counter_strong_cap_enabled: Optional[bool] = Query(None, description="Conviction cap on STRONG counter ON/OFF (force the falling-knife score down)"),
    regime_counter_strong_cap: Optional[float] = Query(None, ge=0, le=100, description="STRONG counter conviction cap value (default 50)"),
    regime_short_release_enabled: Optional[bool] = Query(None, description="SHORT release — on a macro decline, let aligned SHORTs pass (two legs) ON/OFF"),
    regime_short_release_n: Optional[int] = Query(None, ge=1, le=10, description="Macro-decline coin count for SHORT pass (default 6, releases SHORT when RISK_OFF below the MID threshold)"),
    # ── 🦵 [2026-06-11] Per-coin decoupling SHORT release ──
    coin_decouple_enabled: Optional[bool] = Query(None, description="Per-coin decoupling SHORT release — release the weaker leg for a coin that collapsed opposite to BTC ON/OFF (default OFF)"),
    coin_decouple_short_release: Optional[float] = Query(None, ge=0, le=60, description="Bonus for the coin's structural direction on decoupling (offsets the btc -20 hole, default 22)"),
    coin_decouple_long_penalty: Optional[float] = Query(None, ge=0, le=60, description="Penalty for the counter leg (falling knife) on decoupling (default 12)"),
    coin_decouple_min_strength: Optional[float] = Query(None, ge=0, le=1, description="Coin 6TF confidence min (default 0.5, excludes wobble)"),
    coin_decouple_btc_cache_sec: Optional[float] = Query(None, ge=10, le=600, description="BTC 6TF direction cache TTL sec (default 120)"),
    # ── 🦵🌊 [2026-06-12 owner] Momentum decouple — a leading version of coin_decouple (detects an up inflection, releases conviction) ──
    mom_decouple_enabled: Optional[bool] = Query(None, description="Momentum decouple — at a top, when a coin's momentum dies alone, release the weaker leg's conviction ON/OFF (default OFF)"),
    mom_decouple_weight: Optional[float] = Query(None, ge=0, le=60, description="Conviction adjustment scale W (default 30, flips a 50-point gap)"),
    mom_decouple_cap: Optional[float] = Query(None, ge=0, le=60, description="Output clamp ±cap (default 35)"),
    mom_decouple_base: Optional[float] = Query(None, ge=0, le=1, description="Base adjustment from position alone (default 0.45)"),
    mom_decouple_up_thr: Optional[float] = Query(None, ge=0, le=1, description="Momentum |up| min — below this is not a rollover (default 0.40)"),
    mom_decouple_div_thr: Optional[float] = Query(None, ge=0, le=2, description="Min divergence vs BTC momentum — excludes market-wide pullbacks (default 0.20)"),
    mom_decouple_pos_hi: Optional[float] = Query(None, ge=0, le=1, description="SHORT release position lower bound (top) (default 0.60)"),
    mom_decouple_pos_lo: Optional[float] = Query(None, ge=0, le=1, description="LONG release position upper bound (bottom) (default 0.40)"),
    mom_decouple_btc_cache_sec: Optional[float] = Query(None, ge=10, le=600, description="BTC 5m momentum cache TTL sec (default 60)"),
    # ── 🔄 [2026-06-02 Phase 3] M/W/H&S reversal score ──
    reversal_score: Optional[float] = Query(None, ge=0, le=50, description="Reversal (M/W/H&S) score (default 10). Aligned+/counter−, forming ×0.5"),
    # ── 🕯️ [2026-06-03 owner] TF trend weighting (H4/H1/30M/15M/5M) ──
    h4_trend_weight: Optional[float] = Query(None, ge=0, le=3, description="H4 (4-hour) trend weight (default 1.0, ×6=max)"),
    h1_trend_weight: Optional[float] = Query(None, ge=0, le=3, description="H1 trend weight (default 1.0)"),
    m30_trend_weight: Optional[float] = Query(None, ge=0, le=3, description="30M trend weight (default 1.0)"),
    m15_trend_weight: Optional[float] = Query(None, ge=0, le=3, description="15M trend weight (default 1.0)"),
    m5_trend_weight: Optional[float] = Query(None, ge=0, le=3, description="5M trend weight (default 1.0, 0=off)"),
    breadth_dir_chg1h_pct: Optional[float] = Query(None, ge=0.05, le=2.0, description="breadth direction 1h change-rate threshold % (default 0.3)"),
    breadth_dir_ema_pct: Optional[float] = Query(None, ge=0.02, le=1.0, description="breadth direction 5-min EMA threshold % (default 0.10)"),
    # [2026-05-23 owner] Volatility-reachability gate — "is there enough range to reach TP"
    entry_volatility_gate_enabled: Optional[bool] = Query(None, description="Volatility-reachability gate ON/OFF. Even if the reward distance (resistance) is far, dead volatility can't reach it — blocks dead ranging spots"),
    entry_volatility_lookback_tf: Optional[str] = Query(None, description="TF for measuring range (default 5-min)"),
    entry_volatility_lookback_bars: Optional[int] = Query(None, ge=3, le=100, description="Measure range over the last N bars (default 12 = 1 hour)"),
    entry_volatility_min_reach_ratio: Optional[float] = Query(None, ge=0.1, le=3.0, description="Enter only if recent-range/reward-distance ≥ this ratio (default 0.6)"),
    entry_flip_require_alignment: Optional[bool] = Query(None, description="#2 FLIP alignment: block if the FLIP direction is opposite to both H1+30M ON/OFF"),
    # ── ★ Long Hold Persistence (2026-04-26 owner "can't leave without a profit") ──
    trend_reversal_enabled: Optional[bool] = Query(None, description="Trend-reversal auto exit ON/OFF"),
    bb_macd_sw_enabled: Optional[bool] = Query(None, description="SIDEWAYS BB+MACD auto exit ON/OFF"),
    bb_macd_sw_min_hold_hours: Optional[float] = Query(None, ge=0.1, le=99.0, description="bb_macd_sw trigger min hold (h)"),
    bb_macd_sw_pnl_low: Optional[float] = Query(None, ge=-99.0, le=0.0, description="bb_macd_sw trigger pnl lower bound (%)"),
    bb_macd_sw_pnl_high: Optional[float] = Query(None, ge=0.0, le=99.0, description="bb_macd_sw trigger pnl upper bound (%)"),
    caution_sideways_profit_secure_enabled: Optional[bool] = Query(None, description="Ranging + in-profit auto take-profit ON/OFF"),
    caution_min_hold_sec: Optional[float] = Query(None, ge=0, le=86400, description="caution trigger min hold (sec)"),
    caution_fee_rate: Optional[float] = Query(None, ge=0.0, le=0.01, description="caution fee rate"),
    caution_min_profit_multiplier: Optional[float] = Query(None, ge=0.1, le=100.0, description="caution min net profit = fee × N"),
    quick_tp_enabled: Optional[bool] = Query(None, description="Time-based quick TP ON/OFF"),
    quick_tp_min_hold_hours: Optional[float] = Query(None, ge=0.1, le=999.0, description="quick_tp trigger min hold (h)"),
    quick_tp_min_pnl_pct: Optional[float] = Query(None, ge=0.0, le=99.0, description="quick_tp trigger min pnl (%)"),
    btc_crash_threshold_pct: Optional[float] = Query(None, ge=-99.0, le=0.0, description="BTC crash auto-exit threshold (%)"),
    btc_emergency_pause_enabled: Optional[bool] = Query(None, description="BTC sudden-move detection ON/OFF"),
    btc_emergency_pause_threshold_pct: Optional[float] = Query(None, ge=0.5, le=99.0, description="Trigger threshold (absolute %, default 5)"),
    btc_emergency_pause_window_min: Optional[float] = Query(None, ge=1.0, le=120.0, description="Check window (min, default 10)"),
    btc_emergency_mode: Optional[str] = Query(None, description="Mode: trend_aligned/pause/close_all"),
    btc_emergency_aggressive_entry: Optional[bool] = Query(None, description="Accelerate trend-direction entries into empty slots ON/OFF"),
    btc_emergency_aligned_duration_min: Optional[float] = Query(None, ge=1.0, le=1440.0, description="Trend-alignment hold time (min, default 120=2h)"),
    # ★ [2026-04-26] Winners-Only Add — owner's "true Autocoin"
    winners_add_enabled: Optional[bool] = Query(None, description="Winners Add ON/OFF — add to favorable coins as capital grows"),
    winners_add_capital_threshold_pct: Optional[float] = Query(None, ge=1.0, le=99.0, description="Trigger threshold (equity +N% increase)"),
    winners_add_min_pnl_pct: Optional[float] = Query(None, ge=0.0, le=99.0, description="Top-priority pnl threshold (%)"),
    winners_add_max_per_event: Optional[int] = Query(None, ge=1, le=10, description="Max coins per trigger"),
    winners_add_max_pct_per_coin: Optional[float] = Query(None, ge=1.0, le=999.0, description="Max add per coin = existing margin × N%"),
    winners_add_cooldown_sec: Optional[float] = Query(None, ge=60, le=86400, description="Trigger cooldown (sec)"),
    min_sl_pct: Optional[float] = Query(None, ge=0.0001, le=0.5, description="SL min distance (price ratio, 0.001=0.1%)"),
    max_sl_distance_pct: Optional[float] = Query(None, ge=0.5, le=99.9, description="SL max distance (%, 99=effectively disabled)"),
    max_atr_pct: Optional[float] = Query(None, ge=0.5, le=99.0, description="ATR cap (%, protects high-volatility coins)"),
    cycle_min_rr: Optional[float] = Query(None, ge=0.1, le=10.0, description="TP/SL min RR (1.0=guard disabled)"),
    # ── Min TP fee-guard (2026-05-15 owner, prevents an immediate TP hit + fee loss right after entry) ──
    min_tp_distance_enabled: Optional[bool] = Query(None, description="Min TP fee-guard: forbid TP right next to entry (protects low-volatility coins)"),
    min_tp_distance_pct: Optional[float] = Query(None, ge=0.05, le=2.0, description="Min TP distance (%, round-trip fee 0.11%×~3=0.30)"),
    # ── 5m Microtiming Gate (2026-05-16 owner, "WAIT, don't BLOCK — get in at the exact spot") ──
    microtiming_5m_enabled: Optional[bool] = Query(None, description="5m RSI/MACD/BB micro-timing gate (defer, not BLOCK)"),
    microtiming_5m_min_score: Optional[int] = Query(None, ge=1, le=3, description="Enter when ≥ N of the 3 are met (default 2)"),
    microtiming_5m_defer_sec: Optional[float] = Query(None, ge=60.0, le=3600.0, description="Re-evaluation interval after defer (s)"),
    microtiming_5m_max_defers: Optional[int] = Query(None, ge=1, le=10, description="Max defer count (expires naturally if exceeded)"),
    microtiming_5m_rsi_long_threshold: Optional[float] = Query(None, ge=10.0, le=50.0, description="LONG: previous RSI ≤ this + upward inflection"),
    microtiming_5m_rsi_short_threshold: Optional[float] = Query(None, ge=50.0, le=90.0, description="SHORT: previous RSI ≥ this + downward inflection"),
    microtiming_5m_bb_low_pct: Optional[float] = Query(None, ge=0.0, le=50.0, description="BB lower-zone threshold % (LONG previous position)"),
    microtiming_5m_bb_recover_pct: Optional[float] = Query(None, ge=0.0, le=80.0, description="BB recovery threshold % (LONG current position)"),
    microtiming_5m_phase_k_exempt: Optional[bool] = Query(None, description="Phase K (regime transition) entry exemption"),
    # ── DrawdownShield base (2026-05-16 owner, fixes unrealized swings blocking other entries) ──
    drawdown_shield_use_cash_only: Optional[bool] = Query(None, description="DrawdownShield: True=cash only (ignore UPL), False=equity (include UPL, existing)"),
    drawdown_shield_caution_pct: Optional[float] = Query(None, ge=0, le=100, description="DrawdownShield cumulative CAUTION threshold (%, default 5)"),
    drawdown_shield_defend_pct: Optional[float] = Query(None, ge=0, le=100, description="Cumulative DEFEND threshold (%, default 10)"),
    drawdown_shield_crisis_pct: Optional[float] = Query(None, ge=0, le=100, description="Cumulative CRISIS threshold (%, default 20)"),
    drawdown_shield_caution_usd: Optional[float] = Query(None, ge=0, le=100000, description="Daily CAUTION threshold ($, default 30)"),
    drawdown_shield_defend_usd: Optional[float] = Query(None, ge=0, le=100000, description="Daily DEFEND threshold ($, default 60)"),
    drawdown_shield_crisis_usd: Optional[float] = Query(None, ge=0, le=100000, description="Daily CRISIS threshold ($, default 100)"),
    drawdown_shield_caution_pen: Optional[float] = Query(None, ge=-100, le=0, description="CAUTION conviction penalty (negative, default -10)"),
    drawdown_shield_defend_pen: Optional[float] = Query(None, ge=-100, le=0, description="DEFEND penalty (default -20)"),
    drawdown_shield_crisis_pen: Optional[float] = Query(None, ge=-100, le=0, description="CRISIS penalty (default -30)"),
    # ── [2026-05-16 owner] Same-coin Flip Cooldown + 5m Raw Body Guard + Imminent Flip ──
    same_coin_flip_cooldown_enabled: Optional[bool] = Query(None, description="Same-coin LONG↔SHORT new-entry N-min cooldown ON/OFF"),
    same_coin_flip_cooldown_min: Optional[int] = Query(None, ge=0, le=600, description="Cooldown minutes (60=default)"),
    # ── ★ [2026-06-05 owner] 1M micro-check ──
    micro_1m_check_enabled: Optional[bool] = Query(None, description="1M micro-check ON/OFF — verify 1-min bar timing right before entry"),
    micro_1m_candle_check: Optional[bool] = Query(None, description="① Check the last 1M bar's direction"),
    micro_1m_candle_trend_exempt_adx: Optional[float] = Query(None, ge=0, le=100, description="If the trend is strong (ADX ≥ this = wall-riding), exempt the 1M bar direction → prevents entry delay (0=disabled, e.g. 30)"),
    micro_1m_volume_check: Optional[bool] = Query(None, description="② Check for consecutive 1M volume decline"),
    micro_1m_rsi_check: Optional[bool] = Query(None, description="③ Check 1M RSI extremes"),
    micro_1m_rsi_long_max: Optional[float] = Query(None, ge=50, le=90, description="LONG RSI overheat threshold (default 70)"),
    micro_1m_rsi_short_min: Optional[float] = Query(None, ge=10, le=50, description="SHORT RSI overheat threshold (default 30)"),
    micro_1m_vol_decline_bars: Optional[int] = Query(None, ge=2, le=10, description="Consecutive volume-decline bars (default 3)"),
    raw_body_guard_enabled: Optional[bool] = Query(None, description="5m raw-body guard ON/OFF — BLOCK if the last N bars' open→close net sign is opposite"),
    raw_body_guard_lookback: Optional[int] = Query(None, ge=1, le=20, description="lookback 5m bars (3=default)"),
    raw_body_guard_min_net_pct: Optional[float] = Query(None, ge=0.0, le=5.0, description="min net % (0=sign only, 0.15~0.30 recommended)"),
    # ── [2026-05-16 owner vision] Momentum Derivative Guard (first derivative of RSI/MACD flow) ──
    momentum_deriv_guard_enabled: Optional[bool] = Query(None, description="RSI/MACD hist change-rate guard ON/OFF — BLOCK if the flow opposes the entry direction"),
    momentum_deriv_guard_tf: Optional[str] = Query(None, description="TF (1/5/15/30/60), default 5"),
    momentum_deriv_guard_lookback: Optional[int] = Query(None, ge=2, le=50, description="Comparison window bars (5=default)"),
    momentum_deriv_guard_rsi_min_slope: Optional[float] = Query(None, ge=0.0, le=50.0, description="RSI Δ threshold (absolute, 2.0=default)"),
    momentum_deriv_guard_macd_min_slope: Optional[float] = Query(None, ge=0.0, le=10.0, description="MACD hist Δ threshold (0=sign only)"),
    momentum_deriv_guard_require_both: Optional[bool] = Query(None, description="True=BLOCK only if both RSI+MACD are opposite, False=BLOCK if either is opposite"),
    # ── [2026-05-16 owner vision #2] MTF Momentum Alignment (consistency of acceleration across TFs) ──
    mtf_momentum_align_enabled: Optional[bool] = Query(None, description="MTF momentum-alignment guard ON/OFF — whether the TFs' acceleration direction matches the entry direction"),
    mtf_momentum_align_tfs: Optional[str] = Query(None, description="TFs CSV (e.g. '60,30,5')"),
    mtf_momentum_align_lookback: Optional[int] = Query(None, ge=2, le=50, description="Comparison window bars per TF"),
    mtf_momentum_align_min_aligned: Optional[int] = Query(None, ge=1, le=10, description="Min matching TFs (e.g. 2 of 3)"),
    mtf_momentum_align_rsi_slope_thr: Optional[float] = Query(None, ge=0.0, le=20.0, description="RSI Δ sign-determination threshold"),
    mtf_momentum_align_use_macd: Optional[bool] = Query(None, description="True=a TF is aligned only if both RSI+MACD match"),
    # ── [2026-05-16 owner vision #3] CFID — Coin Flip Imminent Detector ──
    cfid_enabled: Optional[bool] = Query(None, description="Per-coin imminent-inflection detection ON/OFF"),
    cfid_tf: Optional[str] = Query(None, description="TF (60=H1, 30=30M recommended)"),
    cfid_ema_gap_thr_pct: Optional[float] = Query(None, ge=0.05, le=5.0, description="EMA20-50 gap/price*100 threshold"),
    cfid_volume_spike_ratio: Optional[float] = Query(None, ge=1.0, le=10.0, description="Last N bars vol avg / prior N bars spike ratio"),
    cfid_adx_change_min: Optional[float] = Query(None, ge=0.1, le=20.0, description="ADX change-rate absolute-value threshold"),
    cfid_lookback: Optional[int] = Query(None, ge=3, le=50, description="Comparison window bars"),
    cfid_bypass_momentum_deriv: Optional[bool] = Query(None, description="Allow bypassing the momentum_deriv guard"),
    cfid_bypass_mtf_align: Optional[bool] = Query(None, description="Allow bypassing the mtf_momentum_align guard"),
    # ── ★ [2026-05-18 owner vision #5] Leading Entry ──
    leading_entry_mode: Optional[str] = Query(None, description="Leading-entry mode: 'OFF' / 'CFID' / 'PATTERN' (mutually exclusive)"),
    cfid_leading_min_strength: Optional[float] = Query(None, ge=10.0, le=100.0, description="[CFID mode] CFID strength threshold (default 70)"),
    cfid_leading_size_pct: Optional[float] = Query(None, ge=0.5, le=50.0, description="[CFID mode] entry size % of equity (default 5)"),
    cfid_leading_bypass_microtiming: Optional[bool] = Query(None, description="[CFID mode] bypass the 5m microtiming gate"),
    cfid_leading_bypass_bb_regime: Optional[bool] = Query(None, description="[CFID mode] bypass the BB_REGIME peak/trough block"),
    pattern_leading_size_pct: Optional[float] = Query(None, ge=0.5, le=50.0, description="[PATTERN mode] entry size % of equity (default 5)"),
    pattern_leading_min_5step_score: Optional[int] = Query(None, ge=1, le=12, description="[PATTERN mode] threshold out of the 5step max of 12 (default 6)"),
    pattern_leading_max_sr_pct: Optional[float] = Query(None, ge=0.1, le=10.0, description="[PATTERN mode] sr_near_S/R distance % (default 1.0)"),
    pattern_leading_min_mtf_align: Optional[int] = Query(None, ge=1, le=4, description="[PATTERN mode] mtf_align matching TFs (default 2)"),
    pattern_leading_bypass_microtiming: Optional[bool] = Query(None, description="[PATTERN mode] bypass the 5m microtiming gate"),
    pattern_leading_bypass_bb_regime: Optional[bool] = Query(None, description="[PATTERN mode] bypass the BB_REGIME peak/trough block"),
    # ── ★ [2026-05-19 Phase 6 Step 2 B-Full] Combinatorial Weighting ──
    #   Bonuses + trigger thresholds for the 4 combos (A/B/C/D). Adjustable from presets/UI.
    phase6_combo_a_bonus: Optional[int] = Query(None, ge=0, le=50, description="[combo A] bonus when PA+zone+MTF align (default 25)"),
    phase6_combo_a_sr_min: Optional[int] = Query(None, ge=0, le=10, description="[combo A] sr_s min (8=near only, 5=up to mid, default 5)"),
    phase6_combo_a_mtf_min: Optional[int] = Query(None, ge=0, le=4, description="[combo A] mtf_s min (4=strong alignment, 2=partial, default 2)"),
    phase6_combo_b_bonus: Optional[int] = Query(None, ge=0, le=70, description="[combo B] bonus on CFID+EMA+vol (default 35)"),
    phase6_combo_b_strength_min: Optional[int] = Query(None, ge=0, le=100, description="[combo B] CFID strength min (70=strong, 50=medium, default 50)"),
    phase6_combo_c_bonus: Optional[int] = Query(None, ge=0, le=40, description="[combo C] bonus on 5step+vol (default 15)"),
    phase6_combo_c_5step_min: Optional[int] = Query(None, ge=0, le=12, description="[combo C] 5step score min (10=max, 7=strong spot, default 7)"),
    phase6_combo_d_bonus: Optional[int] = Query(None, ge=0, le=40, description="[combo D] bonus when news is strong+aligned (default 15)"),
    phase6_combo_d_news_abs_min: Optional[int] = Query(None, ge=0, le=20, description="[combo D] |news_raw| min (10=strong, 6=medium, default 6)"),
    # ── ★ [2026-05-19 owner decision] BB block-guard thresholds (UI-adjustable, preserves the ORDI lesson) ──
    bb_block_threshold_pct: Optional[float] = Query(None, ge=50.0, le=100.0, description="[BB hardblock] block LONG > this (default 85, SHORT symmetric < 100-this)"),
    bb_penalty_threshold_pct: Optional[float] = Query(None, ge=50.0, le=100.0, description="[BB penalty] penalize conv for LONG > this (default 75, SHORT symmetric < 100-this)"),
    bb_penalty_amount: Optional[float] = Query(None, ge=0.0, le=50.0, description="[BB penalty amount] penalty in 100-scale points (default 20)"),
    # ── [2026-05-16 owner vision #4] Coin State Machine ──
    coin_state_machine_enabled: Optional[bool] = Query(None, description="Classify the coin state into 4 stages at entry ON/OFF"),
    coin_state_apply_conv_adjust: Optional[bool] = Query(None, description="True=apply per-stage adjustment to conviction_score (default OFF)"),
    coin_state_accel_conv_adj: Optional[float] = Query(None, ge=-30, le=30, description="[100-scale] ACCEL adjustment (default 0)"),
    coin_state_steady_conv_adj: Optional[float] = Query(None, ge=-30, le=30, description="[100-scale] STEADY adjustment (default -5)"),
    coin_state_decel_conv_adj: Optional[float] = Query(None, ge=-30, le=30, description="[100-scale] DECEL adjustment (default -10)"),
    coin_state_flip_imminent_conv_adj: Optional[float] = Query(None, ge=-30, le=30, description="[100-scale] FLIP_IMMINENT adjustment (default +5)"),
    # ── [2026-05-16 owner vision #5] Tight Trail After BE ──
    tight_trail_after_be_enabled: Optional[bool] = Query(None, description="When BE lock is active, instant-cut on peak slippage"),
    tight_trail_max_slippage_pct: Optional[float] = Query(None, ge=0.05, le=5.0, description="Cut when it drops N%p from peak (0.2=default)"),
    tight_trail_min_peak_pct: Optional[float] = Query(None, ge=0.1, le=10.0, description="Apply only when peak ≥ this (0.4=default)"),
    tight_trail_atr_adaptive_enabled: Optional[bool] = Query(None, description="ATR-proportional dynamic slippage threshold"),
    tight_trail_atr_tf: Optional[str] = Query(None, description="ATR TF (5=5m default)"),
    tight_trail_atr_period: Optional[int] = Query(None, ge=5, le=50, description="ATR period (14=default)"),
    tight_trail_atr_multiplier: Optional[float] = Query(None, ge=0.05, le=2.0, description="atr_pct × N → slippage (0.3=default)"),
    tight_trail_atr_cap_pct: Optional[float] = Query(None, ge=0.1, le=5.0, description="Adaptive slippage cap (0.6=default)"),
    # ── 🎯 [2026-06-12 owner ESPORTS/WLD] Per-coin trend-adaptive exit ──
    trend_adaptive_exit_enabled: Optional[bool] = Query(None, description="Per-coin trend-adaptive exit — let runners run and scalp choppers (ADX-based, default OFF)"),
    trend_adaptive_exit_adx_strong: Optional[float] = Query(None, ge=10, le=60, description="ADX ≥ this = relax the runner trail (default 30)"),
    trend_adaptive_exit_adx_weak: Optional[float] = Query(None, ge=5, le=40, description="ADX ≤ this = tighten the chopper trail (default 18)"),
    trend_adaptive_exit_runner_factor: Optional[float] = Query(None, ge=0.1, le=1.0, description="runner factor <1 (preserve↓/slip↑=let it run, default 0.6)"),
    trend_adaptive_exit_chop_factor: Optional[float] = Query(None, ge=1.0, le=3.0, description="chopper factor >1 (preserve↑/slip↓=scalp, default 1.4)"),
    trend_adaptive_exit_adx_cache_sec: Optional[float] = Query(None, ge=5, le=300, description="Coin ADX cache TTL sec (default 30)"),
    imminent_flip_enabled: Optional[bool] = Query(None, description="Release the freeze on an imminent-flip signal even within the freeze window"),
    imminent_flip_ema_gap_pct: Optional[float] = Query(None, ge=0.0, le=10.0, description="BTC EMA20-50 gap threshold (0.3=default)"),
    imminent_flip_use_30m: Optional[bool] = Query(None, description="Use the 30M auxiliary signal"),
    imminent_flip_adx_rise_min: Optional[float] = Query(None, ge=0.0, le=50.0, description="ADX rise magnitude (2.0=default)"),
    imminent_flip_gap_lookback: Optional[int] = Query(None, ge=1, le=20, description="gap/ADX comparison bars (3=default)"),
    # ── Hard ROE Cap (force-cut at max loss ROE per position, 2026-04-25) ──
    hard_roe_cap_enabled: Optional[bool] = Query(None, description="Hard ROE Cap ON/OFF — force-cut a position when it hits the ROE threshold"),
    hard_roe_cap_roe_pct: Optional[float] = Query(None, ge=-99.0, le=0.0, description="Hard ROE Cap threshold ROE % (negative, e.g. -8.0)"),
    # ── Leverage Tier (ATR-based tiered leverage, 2026-04-25) ──
    leverage_tier_enabled: Optional[bool] = Query(None, description="Leverage Tier ON/OFF (ATR-based tiering)"),
    leverage_tier_atr_low_pct: Optional[float] = Query(None, ge=0.5, le=5.0, description="Low-tier ATR threshold (%) — below=low lev"),
    leverage_tier_low: Optional[int] = Query(None, ge=2, le=20, description="Low-tier leverage multiplier"),
    leverage_tier_atr_high_pct: Optional[float] = Query(None, ge=1.0, le=5.0, description="High-tier ATR threshold (%) — at/above=high lev"),
    leverage_tier_high: Optional[int] = Query(None, ge=2, le=20, description="High-tier leverage multiplier"),
    # ── 30M Thesis Invalidation ──
    thesis_invalidation_enabled: Optional[bool] = Query(None, description="30M structural-shift monitoring ON/OFF"),
    thesis_invalidation_min_hold_h: Optional[float] = Query(None, ge=0.5, le=4.0, description="Min hold time (hours)"),
    thesis_invalidation_max_peak_pct: Optional[float] = Query(None, ge=0.1, le=1.0, description="Peak-profit threshold (%)"),
    # ── Morning Shield / Guard ──
    morning_shield_enabled: Optional[bool] = Query(None, description="Morning Shield (protect overnight profits) ON/OFF"),
    morning_guard_enabled: Optional[bool] = Query(None, description="Morning Guard (restrict morning entries) ON/OFF"),
    morning_shield_lock_pct: Optional[float] = Query(None, ge=10, le=90, description="Profit-secure rate (%)"),
    morning_guard_conviction_boost: Optional[float] = Query(None, ge=0, le=50, description="[100-scale] morning conviction raise (default 15)"),
    morning_guard_end_hour_kst: Optional[float] = Query(None, ge=7.0, le=12.0, description="Guard end time (KST)"),
    event_shield_enabled: Optional[bool] = Query(None, description="Event Shield (economic-event shield) ON/OFF"),
    event_shield_times_kst: Optional[str] = Query(None, description="Event times CSV ('2026-06-10 21:30, ...') KST"),
    event_shield_window_min: Optional[float] = Query(None, ge=0, le=180, description="Post-event window (min)"),
    event_shield_lead_min: Optional[float] = Query(None, ge=0, le=120, description="Slippage lead — before an event use window+lead minutes (ahead of the crowd)"),
    event_shield_lock_pct: Optional[float] = Query(None, ge=10, le=95, description="Profit-preservation rate when tightening SL for an event (%)"),
    event_shield_auto_fetch: Optional[bool] = Query(None, description="Auto-fetch ForexFactory USD High-impact events ON/OFF"),
    auto_tp_enabled: Optional[bool] = Query(None, description="Auto Take-Profit (trailing harvest) ON/OFF"),
    auto_tp_usdt: Optional[float] = Query(None, ge=0, description="Arm threshold (once net profit exceeds this, protect that profit, USDT)"),
    auto_tp_peak_giveback_pct: Optional[float] = Query(None, ge=0, le=1, description="After arming, harvest when it gives back this ratio from peak net profit (0~1)"),
    auto_sl_pct_enabled: Optional[bool] = Query(None, description="Auto Stop-Loss (auto-cut at N% loss) ON/OFF — usually OFF"),
    auto_sl_pct: Optional[float] = Query(None, ge=0, le=100, description="Cut loss rate (%)"),
    dual_direction_observe: Optional[bool] = Query(None, description="Dual-direction evaluation Phase 1 observation (no entry change, records the opposite-direction shadow score) ON/OFF"),
    dual_direction_enabled: Optional[bool] = Query(None, description="Dual-direction evaluation Phase 2 — pick the actual entry direction as the higher one (when direction_mode=both) ON/OFF"),
    # ── Erosion Guard ──
    erosion_guard_enabled: Optional[bool] = Query(None, description="Erosion Guard (prevent profit erosion) ON/OFF"),
    erosion_guard_peak_pct: Optional[float] = Query(None, ge=0.1, le=3.0, description="peak min (%)"),
    erosion_guard_ratio: Optional[float] = Query(None, ge=0.1, le=0.9, description="Erosion-ratio trigger"),
    # ── SL Dodge ──
    sl_dodge_enabled: Optional[bool] = Query(None, description="SL Dodge ON/OFF"),
    sl_dodge_proximity_pct: Optional[float] = Query(None, ge=0.5, le=5.0, description="SL-proximity threshold (%)"),
    sl_dodge_retreat_pct: Optional[float] = Query(None, ge=0.5, le=5.0, description="Retreat ratio (%)"),
    sl_dodge_max_count: Optional[int] = Query(None, ge=1, le=10, description="Max dodge count"),
    sl_dodge_max_total_pct: Optional[float] = Query(None, ge=1.0, le=20.0, description="Total dodge cap (%)"),
    # ── SL Decay ──
    sl_decay_enabled: Optional[bool] = Query(None, description="SL Decay (shrink SL over time) ON/OFF"),
    sl_decay_2h_ratio: Optional[float] = Query(None, ge=0.1, le=1.0, description="SL ratio after 2 hours"),
    sl_decay_3h_ratio: Optional[float] = Query(None, ge=0.1, le=1.0, description="SL ratio after 3 hours"),
    # ── Fast-Reject ──
    fast_reject_enabled: Optional[bool] = Query(None, description="Fast-Reject (early stop-loss) ON/OFF"),
    fast_reject_min_sec: Optional[float] = Query(None, ge=60, le=3600, description="Trigger min hold (sec)"),
    fast_reject_max_sec: Optional[float] = Query(None, ge=60, le=3600, description="Trigger max hold (sec)"),
    fast_reject_peak_threshold_pct: Optional[float] = Query(None, ge=0.0, le=2.0, description="peak-shortfall threshold (%)"),
    fast_reject_trigger_pnl_pct: Optional[float] = Query(None, ge=-5.0, le=0.0, description="Trigger pnl threshold (%, negative)"),
    # ── Entry Quality Filter ──
    entry_quality_enabled: Optional[bool] = Query(None, description="Entry Quality Filter (M5 micro-timing) ON/OFF"),
    eq_momentum_enabled: Optional[bool] = Query(None, description="M5 momentum filter ON/OFF"),
    eq_momentum_count: Optional[int] = Query(None, ge=1, le=5, description="Bars to inspect"),
    eq_momentum_min_agree: Optional[int] = Query(None, ge=1, le=5, description="Min matching bars"),
    eq_bb_enabled: Optional[bool] = Query(None, description="BB-position filter ON/OFF"),
    eq_bb_upper_pct: Optional[float] = Query(None, ge=50, le=99, description="LONG-block BB% upper bound"),
    eq_bb_lower_pct: Optional[float] = Query(None, ge=1, le=50, description="SHORT-block BB% lower bound"),
    eq_nbar_enabled: Optional[bool] = Query(None, description="N-bar trend filter ON/OFF"),
    eq_nbar_count: Optional[int] = Query(None, ge=3, le=10, description="Trend-inspection bars"),
    eq_nbar_min_ratio: Optional[float] = Query(None, ge=0.3, le=1.0, description="HH/LH min ratio"),
    # ── Advanced ──
    rr_ratio: Optional[float] = Query(None, ge=1.0, le=10.0, description="Risk-Reward ratio"),
    adaptive_cooldown: Optional[bool] = Query(None, description="Adaptive cooldown ON/OFF"),
    emergency_tp_tiers: Optional[bool] = Query(None, description="Emergency TP tier ON/OFF"),
    coin_repeat_window_hours: Optional[float] = Query(None, ge=1, le=72, description="Repeat-brake window (hours)"),
    scanner_blacklist: Optional[str] = Query(None, description="Scanner blacklist (comma-separated, e.g. CLUSDT,ABCUSDT)"),
    # ── Manual Exit Penalty (manual-exit cooldown) ──
    manual_exit_penalty_enabled: Optional[bool] = Query(None, description="Re-entry cooldown on manual exit ON/OFF (OFF=always exempt)"),
    manual_exit_penalty_hours: Optional[float] = Query(None, ge=0.0, le=24.0, description="Cooldown time on a losing exit (hours)"),
    phase3_context_bonus_enabled: Optional[bool] = Query(None, description="Phase 3 time-of-day (±4) + coin (+2) bonus ON/OFF"),
    # ── [2026-05-19] 124 advanced hidden settings — Query params ──
    # A. Core TF
    primary_tf: Optional[str] = Query(None, description="Primary TF (60=H1, 240=H4)"),
    entry_tf: Optional[str] = Query(None, description="Entry TF (5=M5)"),
    # ★ [2026-05-31 owner server-b lock_market race real fix] /config POST ignored lock_market → even sending an empty string left the server unchanged → stuck forever.
    lock_market: Optional[str] = Query(None, description="Lock to single market (empty=auto-scan / Unlock)"),
    # B. Post-Trade Pause
    post_trade_pause_enabled: Optional[bool] = Query(None, description="Post-trade cooldown ON/OFF"),
    post_trade_pause_profit_sec: Optional[float] = Query(None, ge=0, le=14400, description="Cooldown after take-profit (sec)"),
    post_trade_pause_loss_sec: Optional[float] = Query(None, ge=0, le=14400, description="Cooldown after stop-loss (sec)"),
    post_trade_pause_fastreject_sec: Optional[float] = Query(None, ge=0, le=14400, description="Cooldown after Fast Reject (sec)"),
    post_trade_pause_loss_sliding_enabled: Optional[bool] = Query(None, description="Sliding loss cooldown ON/OFF"),
    post_trade_pause_loss_tier1_pct: Optional[float] = Query(None, ge=0, le=100, description="Tier1 loss %"),
    post_trade_pause_loss_tier1_sec: Optional[float] = Query(None, ge=0, le=14400, description="Tier1 cooldown (sec)"),
    post_trade_pause_loss_tier2_pct: Optional[float] = Query(None, ge=0, le=100, description="Tier2 loss %"),
    post_trade_pause_loss_tier2_sec: Optional[float] = Query(None, ge=0, le=14400, description="Tier2 cooldown (sec)"),
    post_trade_pause_loss_tier3_pct: Optional[float] = Query(None, ge=0, le=100, description="Tier3 loss %"),
    post_trade_pause_loss_tier3_sec: Optional[float] = Query(None, ge=0, le=14400, description="Tier3 cooldown (sec)"),
    post_trade_pause_loss_tier4_pct: Optional[float] = Query(None, ge=0, le=100, description="Tier4 loss %"),
    post_trade_pause_loss_tier4_sec: Optional[float] = Query(None, ge=0, le=86400, description="Tier4 cooldown (sec)"),
    post_trade_pause_loss_tier5_sec: Optional[float] = Query(None, ge=0, le=86400, description="Tier5 cooldown (sec)"),
    # C. Direction Exhaustion
    direction_exhaustion_enabled: Optional[bool] = Query(None, description="Direction-exhaustion block ON/OFF"),
    direction_exhaustion_window_sec: Optional[float] = Query(None, ge=60, le=14400, description="Monitoring window (sec)"),
    direction_exhaustion_profit_count: Optional[int] = Query(None, ge=1, le=10, description="Take-profit count threshold"),
    direction_exhaustion_block_sec: Optional[float] = Query(None, ge=60, le=14400, description="Block duration (sec)"),
    # D. Coin Reentry Penalty
    coin_reentry_penalty_enabled: Optional[bool] = Query(None, description="Re-entry penalty ON/OFF"),
    coin_reentry_penalty_window_sec: Optional[float] = Query(None, ge=60, le=14400, description="Monitoring window (sec)"),
    coin_reentry_penalty_per_count: Optional[float] = Query(None, ge=0, le=50, description="Per-occurrence penalty (100-scale)"),
    # E. Trailing TP
    trailing_tp_enabled: Optional[bool] = Query(None, description="Trailing TP ON/OFF"),
    trailing_tp_min_progress: Optional[float] = Query(None, ge=0, le=1, description="Trigger progress"),
    trailing_tp_follow_low: Optional[float] = Query(None, ge=0.5, le=1, description="Low-volatility trailing rate"),
    trailing_tp_follow_mid: Optional[float] = Query(None, ge=0.5, le=1, description="Medium-volatility trailing rate"),
    trailing_tp_follow_high: Optional[float] = Query(None, ge=0.5, le=1, description="High-volatility trailing rate"),
    # F. Portfolio SL Rate
    portfolio_sl_rate_enabled: Optional[bool] = Query(None, description="Portfolio SL ratio ON/OFF"),
    portfolio_sl_rate_window_min: Optional[int] = Query(None, ge=1, le=60, description="Monitoring window (min)"),
    portfolio_sl_rate_threshold: Optional[int] = Query(None, ge=1, le=20, description="SL count threshold"),
    portfolio_sl_rate_pause_min: Optional[int] = Query(None, ge=1, le=360, description="Pause time (min)"),
    # G. BTC+B12 Combined Cap
    btc_b12_combined_cap_enabled: Optional[bool] = Query(None, description="BTC+B12 simultaneous cap ON/OFF"),
    btc_b12_combined_cap_max: Optional[int] = Query(None, ge=1, le=10, description="Combined max slots"),
    # H. Override Slot
    override_min_adx: Optional[float] = Query(None, ge=0, le=100, description="Override min ADX"),
    override_min_mtf_align: Optional[int] = Query(None, ge=0, le=6, description="Override min MTF align"),
    override_min_b12_n: Optional[int] = Query(None, ge=0, le=20, description="Override min B12 N"),
    override_require_btc_trend_match: Optional[bool] = Query(None, description="Require Override BTC trend match"),
    override_max_extra_slots: Optional[int] = Query(None, ge=0, le=10, description="Override max extra slots"),
    override_breakeven_trigger_pct: Optional[float] = Query(None, ge=0, le=5, description="Override BE trigger (%)"),
    # J. Pair Block
    pair_block_enabled: Optional[bool] = Query(None, description="Pair Block ON/OFF"),
    pair_block_mode: Optional[str] = Query(None, description="Pair Block mode (aggressive/conservative)"),
    pair_block_same_limit: Optional[int] = Query(None, ge=1, le=10, description="Max pairs in the same direction"),
    # K. Coin Profit Lock-in
    coin_profit_lockin_enabled: Optional[bool] = Query(None, description="Profit Lock-in ON/OFF"),
    coin_profit_lockin_window_hours: Optional[float] = Query(None, ge=0.5, le=48, description="Protection time (h)"),
    coin_profit_lockin_min_realized: Optional[float] = Query(None, ge=0, le=1000, description="Trigger min realized profit ($)"),
    coin_profit_lockin_protect_ratio: Optional[float] = Query(None, ge=0, le=1, description="Protection ratio"),
    coin_profit_lockin_require_be: Optional[bool] = Query(None, description="Trigger after reaching BE"),
    # L. PA Weight
    pa_weight_enabled: Optional[bool] = Query(None, description="PA Weight ON/OFF"),
    pa_weight_pin_bar: Optional[int] = Query(None, ge=0, le=10, description="PIN_BAR weight"),
    pa_weight_engulfing: Optional[int] = Query(None, ge=0, le=10, description="ENGULFING weight"),
    pa_weight_star_v1: Optional[int] = Query(None, ge=0, le=10, description="STAR_V1 weight"),
    pa_weight_star_v2: Optional[int] = Query(None, ge=0, le=10, description="STAR_V2 weight"),
    pa_weight_squeeze_break: Optional[int] = Query(None, ge=0, le=10, description="SQUEEZE_BREAK weight"),
    pa_weight_bos: Optional[int] = Query(None, ge=0, le=10, description="BOS weight"),
    pa_weight_zone_bonus: Optional[int] = Query(None, ge=0, le=10, description="Zone bonus"),
    pa_zone_proximity_atr: Optional[float] = Query(None, ge=0, le=5, description="Zone ATR multiplier"),
    pa_location_penalty_far: Optional[float] = Query(None, ge=0, le=5, description="Far penalty"),
    # O. Session Profile times
    sess_quiet_start_kst: Optional[float] = Query(None, ge=0, le=24, description="quiet start KST (h)"),
    sess_quiet_end_kst: Optional[float] = Query(None, ge=0, le=24, description="quiet end KST (h)"),
    sess_active_start_kst: Optional[float] = Query(None, ge=0, le=24, description="active start KST (h)"),
    sess_active_end_kst: Optional[float] = Query(None, ge=0, le=24, description="active end KST (h)"),
    # P. Direction Memory details
    dm_window_count: Optional[int] = Query(None, ge=1, le=20, description="DM window count"),
    dm_lookback_days: Optional[float] = Query(None, ge=0.5, le=30, description="DM lookback days"),
    dm_loss_count_penalty: Optional[int] = Query(None, ge=1, le=10, description="DM loss count penalty"),
    dm_cache_ttl_sec: Optional[float] = Query(None, ge=30, le=3600, description="DM cache TTL"),
    # Q. BTC Regime
    btc_regime_ema_short: Optional[int] = Query(None, ge=5, le=100, description="BTC Regime EMA short"),
    btc_regime_ema_long: Optional[int] = Query(None, ge=20, le=200, description="BTC Regime EMA long"),
    btc_regime_trans_band_pct: Optional[float] = Query(None, ge=0, le=10, description="BTC Regime trans band"),
    btc_regime_slope_flat_thr_pct: Optional[float] = Query(None, ge=0, le=5, description="BTC Regime slope flat thr"),
    btc_regime_bull_long_delta: Optional[float] = Query(None, ge=-50, le=50, description="BTC Regime BULL LONG"),
    btc_regime_bull_short_delta: Optional[float] = Query(None, ge=-50, le=50, description="BTC Regime BULL SHORT"),
    btc_regime_bear_short_delta: Optional[float] = Query(None, ge=-50, le=50, description="BTC Regime BEAR SHORT"),
    btc_regime_trans_delta: Optional[float] = Query(None, ge=-50, le=50, description="BTC Regime TRANS"),
    btc_regime_cache_ttl_sec: Optional[float] = Query(None, ge=60, le=3600, description="BTC Regime cache TTL"),
    # R. Market Bias
    mb_lookback_trades: Optional[int] = Query(None, ge=3, le=100, description="MB lookback trades"),
    mb_lookback_hours: Optional[float] = Query(None, ge=1, le=72, description="MB lookback hours"),
    mb_dominance_threshold: Optional[float] = Query(None, ge=0, le=1, description="MB dominance threshold"),
    mb_min_total: Optional[int] = Query(None, ge=1, le=100, description="MB min total"),
    mb_cache_ttl_sec: Optional[float] = Query(None, ge=30, le=3600, description="MB cache TTL"),
    # S. Scanner
    scanner_min_turnover_24h: Optional[float] = Query(None, ge=0, le=100000000, description="Scanner min turnover 24h"),
    scanner_min_price_usdt: Optional[float] = Query(None, ge=0, le=100, description="Scanner min price"),
    scanner_top_n: Optional[int] = Query(None, ge=5, le=100, description="Scanner top N"),
    # T. Misc
    reverse_drift_atr_tf: Optional[str] = Query(None, description="Reverse Drift ATR TF"),
    reverse_drift_atr_period: Optional[int] = Query(None, ge=3, le=50, description="Reverse Drift ATR period"),
    profit_exit_block_min_pnl: Optional[float] = Query(None, ge=0, le=10, description="Profit Exit Block min pnl"),
    # ── Context Engine (2026-04-19) ──
    session_profile_enabled: Optional[bool] = Query(None, description="Session Profile (conviction ± by KST time-of-day) ON/OFF"),
    direction_memory_enabled: Optional[bool] = Query(None, description="Direction Memory (soft penalty on coin+direction losing streaks) ON/OFF"),
    dm_streak_block_enabled: Optional[bool] = Query(None, description="Direction Memory hard block (block entry on N-loss streak) ON/OFF"),
    dm_streak_block: Optional[int] = Query(None, ge=2, le=20, description="Hard-block trigger loss-streak count (default 4)"),
    dm_streak_block_hours: Optional[float] = Query(None, ge=0.1, le=168.0, description="DM Streak hard-block duration (h)"),
    dm_streak_block_opposite: Optional[bool] = Query(None, description="DM Streak: block the opposite direction too (False=allow FLIP)"),
    # ★ Phase F (2026-04-20): Profit Exit Block 3-tuple setting
    profit_exit_block_enabled: Optional[bool] = Query(None, description="B10 Profit Exit Block ON/OFF"),
    profit_exit_block_min_consecutive: Optional[int] = Query(None, ge=2, le=10, description="Trigger win-streak count (default 3)"),
    profit_exit_block_hours: Optional[float] = Query(None, ge=1, le=72, description="Block duration (h)"),
    profit_exit_block_block_opposite: Optional[bool] = Query(None, description="Block the opposite direction too (default False=allow FLIP)"),
    # ── ★ [2026-05-18 owner request] Consecutive Loss Pause (previously missing, added to router/UI) ──
    consecutive_loss_pause_enabled: Optional[bool] = Query(None, description="Auto-pause after N consecutive losses (avoids large cumulative loss)"),
    consecutive_loss_pause_count: Optional[int] = Query(None, ge=2, le=20, description="Trigger loss-streak count (default 3, validation mode recommends 10)"),
    consecutive_loss_pause_min: Optional[int] = Query(None, ge=1, le=1440, description="Pause time minutes (default 60, validation mode recommends 10)"),
    # ── ★ [2026-06-04 owner] Per-direction regime-window failure block ──
    regime_direction_fail_enabled: Optional[bool] = Query(None, description="Per-direction regime-window failure block ON/OFF — N LONG failures within N hours → block LONG only, allow SHORT"),
    regime_direction_fail_window_hours: Optional[float] = Query(None, ge=1, le=24, description="Regime window (hours, default 4.0=one H4 bar)"),
    regime_direction_fail_max: Optional[int] = Query(None, ge=1, le=10, description="Allowed failures (default 3, blocks that direction when exceeded)"),
    btc_regime_enabled: Optional[bool] = Query(None, description="BTC Regime (recognize the BTC primary_tf(H1) EMA regime - counter-direction penalty) ON/OFF"),
    btc_regime_bear_long_delta: Optional[float] = Query(None, ge=-100, le=0, description="[100-scale ×10] BEAR-market LONG-entry penalty (default -20). When set, applies equally to bull_short_delta."),
    market_bias_enabled: Optional[bool] = Query(None, description="Market Bias (recognize multi-coin EXIT skew - counter penalty) ON/OFF"),
    mb_against_delta: Optional[float] = Query(None, ge=-100, le=0, description="[100-scale ×10] skew-counter penalty (default -10)"),
    sess_quiet_delta: Optional[float] = Query(None, ge=-100, le=0, description="[100-scale ×10] KST 01~06h quiet-hours penalty (default -10)"),
    sess_active_delta: Optional[float] = Query(None, ge=0, le=100, description="[100-scale ×10] KST 21~24h active-hours bonus (default +10)"),
    dm_loss_count_delta: Optional[float] = Query(None, ge=-100, le=0, description="[100-scale ×10] same coin×direction losing-streak penalty (default -20)"),
    # ── B11 Regime Direction Lock (2026-04-19 hard block, 3-axis toggle) ──
    regime_direction_lock_enabled: Optional[bool] = Query(None, description="B11 Regime Direction Lock master (BULL→LONG only / BEAR→SHORT only / NEUTRAL→REST)"),
    regime_lock_use_slope: Optional[bool] = Query(None, description="B11 Slope axis — strict EMA20 slope ≥0.3% determination ON/OFF"),
    regime_lock_use_distance: Optional[bool] = Query(None, description="B11 Distance axis — strict distance ≥1.0% from EMA50 determination ON/OFF"),
    regime_lock_use_cross: Optional[bool] = Query(None, description="B11 Direction axis — EMA20 vs EMA50 direction determination ON/OFF (core)"),
    # ── B12 Scanner Breadth Lock (2026-04-23 owner direct request, mutually exclusive with B11) ──
    regime_lock_mode: Optional[str] = Query(None, description="Regime Lock Mode: B11 (BTC EMA) / B12 (Scanner chorus) / OFF (both off)"),
    b12_threshold_n: Optional[int] = Query(None, ge=1, le=20, description="B12 N: min markets pointing the same direction (default 6 = 75% of 8)"),
    b12_window_sec: Optional[float] = Query(None, ge=60.0, le=3600.0, description="B12 vote aggregation window (sec, default 1200=20min, data-analysis based)"),
    # ── Per-direction slot cap (2026-04-20 this agent's instruction, -1=Auto, 0=block, N=explicit) ──
    max_long_positions: Optional[int] = Query(None, ge=-1, le=50, description="LONG slot cap (-1=Auto=max_same_direction, 0=fully blocked, N=explicit)"),
    max_short_positions: Optional[int] = Query(None, ge=-1, le=50, description="SHORT slot cap (-1=Auto=max_same_direction, 0=fully blocked, N=explicit)"),
    auto_first_dir_lock: Optional[bool] = Query(None, description="[2026-04-26] Lock the first-shot direction in Auto mode (true=scalping, false=long-hold both directions free)"),
    # Smartly #2,#4,#5,#6,#7 (2026-04-26 owner stage 1)
    regime_reversal_pause_enabled: Optional[bool] = Query(None),
    regime_reversal_ema_gap_threshold_pct: Optional[float] = Query(None, ge=0.01, le=5.0),
    regime_reversal_adx_threshold: Optional[float] = Query(None, ge=5.0, le=50.0),
    regime_reversal_pause_min: Optional[float] = Query(None, ge=1.0, le=240.0),
    conv_sizing_enabled: Optional[bool] = Query(None),
    conv_sizing_low_threshold: Optional[float] = Query(None, ge=0, le=100),
    conv_sizing_high_threshold: Optional[float] = Query(None, ge=0, le=100),
    conv_risk_scale_enabled: Optional[bool] = Query(None, description="[entry score=conviction] inverse-U risk scaling based on the guard score ON/OFF (default OFF)"),
    conv_risk_peak_conv: Optional[float] = Query(None, ge=0, le=200, description="[entry score=conviction] sweet-spot start/peak conviction (default 65)"),
    conv_risk_peak_mult: Optional[float] = Query(None, ge=0.1, le=3, description="[entry score=conviction] sweet-spot risk multiplier (default 1.5)"),
    conv_risk_chop_conv: Optional[float] = Query(None, ge=0, le=200, description="[entry score=conviction] late line (default 80)"),
    conv_risk_chop_mult: Optional[float] = Query(None, ge=0.1, le=2, description="[entry score=conviction] late risk-cut multiplier (default 0.6)"),
    conv_risk_floor_mult: Optional[float] = Query(None, ge=0.1, le=2, description="[entry score=conviction] below-threshold multiplier (default 0.5)"),
    conv_risk_max_mult: Optional[float] = Query(None, ge=0.5, le=5, description="[entry score=conviction] factor safety cap (default 2.0)"),
    btc_trend_conv_bonus_enabled: Optional[bool] = Query(None),
    btc_trend_conv_bonus: Optional[float] = Query(None, ge=0, le=30),
    winners_add_self_growth_enabled: Optional[bool] = Query(None),
    winners_add_self_growth_pct: Optional[float] = Query(None, ge=0.1, le=20.0),
    winners_add_self_growth_ratio_pct: Optional[float] = Query(None, ge=10.0, le=200.0),
    multi_be_lock_enabled: Optional[bool] = Query(None),
    multi_be_lock_stage1_pct: Optional[float] = Query(None, ge=0.1, le=10.0),
    multi_be_lock_stage2_pct: Optional[float] = Query(None, ge=0.1, le=10.0),
    multi_be_lock_stage3_pct: Optional[float] = Query(None, ge=0.1, le=10.0),
    multi_be_lock_stage4_pct: Optional[float] = Query(None, ge=0.1, le=10.0),
    multi_be_lock_fee_cushion_pct: Optional[float] = Query(None, ge=0.0, le=2.0, description="stage1 fee cushion (default 0.05, prevents small per-trade fee loss after BE)"),
    # ── ★ [2026-06-04 owner] Smart BE Lock ──
    be_lock_smart_rsi_check: Optional[bool] = Query(None, description="① RSI profit-direction check — defer BE if it's running"),
    be_lock_smart_candle_check: Optional[bool] = Query(None, description="② Check the last N bars for a consecutive profit direction — defer BE if accelerating"),
    be_lock_smart_rsi_long_min: Optional[float] = Query(None, ge=40, le=80, description="LONG: RSI ≥ this = running (default 55)"),
    be_lock_smart_rsi_short_max: Optional[float] = Query(None, ge=20, le=60, description="SHORT: RSI ≤ this = running (default 45)"),
    be_lock_smart_candle_count: Optional[int] = Query(None, ge=2, le=10, description="Consecutive profit direction over the last N bars (5M) (default 3)"),
    parent_roe_guard_enabled: Optional[bool] = Query(None),
    parent_max_roe_loss_pct: Optional[float] = Query(None, ge=10.0, le=99.0),
    # ── Phase J v2 (2026-04-21): skip (any direction) while ADX is falling — detects the market cooling ──
    adx_slope_check_enabled: Optional[bool] = Query(None, description="Phase J v2: skip entries (any direction) while ADX is falling ON/OFF"),
    adx_slope_lookback_bars: Optional[int] = Query(None, ge=1, le=10, description="Phase J v2: compare against how many primary_tf(H1) bars ago (default 3=3h)"),
    adx_slope_decline_threshold_pct: Optional[float] = Query(None, ge=0.5, le=30.0, description="Phase J v2: skip on a drop of ≥ N% (default 2.0)"),
    # ── Phase K (2026-04-21): Regime Transition Preemptive Entry ──
    # ⚠️ Mutually exclusive with J v2 — when K=True, J v2 is auto-ignored. On the same ADX-fall signal, K=flip takes priority.
    # When paper_mode=True, only logs to phase_k_paper_log.jsonl without entering.
    regime_transition_enabled: Optional[bool] = Query(None, description="Phase K: Regime Transition Preemptive Entry ON/OFF (⚠️ auto-turns J v2 OFF)"),
    regime_transition_paper_mode: Optional[bool] = Query(None, description="Phase K: paper mode (True=JSONL records only, no entry; False=real entry)"),
    regime_transition_size_mult: Optional[float] = Query(None, ge=0.1, le=0.5, description="Phase K: size multiplier (0.3 floor → 0.5 CAP — finalized in Q4 review)"),
    regime_transition_tp_mult: Optional[float] = Query(None, ge=0.3, le=1.5, description="Phase K: TP multiplier (default 0.7 ultra-short)"),
    regime_transition_sl_mult: Optional[float] = Query(None, ge=0.3, le=1.5, description="Phase K: SL multiplier (default 0.8 tight)"),
    regime_transition_adx_decline_ratio: Optional[float] = Query(None, ge=0.80, le=0.99, description="Phase K: condition adx_now < adx_past × ratio (default 0.95)"),
    regime_transition_ema_gap_threshold_pct: Optional[float] = Query(None, ge=0.1, le=2.0, description="Phase K: BTC |EMA20-50|/price threshold (default 0.3%)"),
    regime_transition_min_conviction: Optional[float] = Query(None, ge=0, le=100, description="[100-scale] Phase K min conviction (default 75)"),
    regime_transition_last_change_age_min: Optional[float] = Query(None, ge=30.0, le=1440.0, description="Phase K: min age after a regime transition (min, default 180)"),
    regime_transition_daily_fail_limit: Optional[int] = Query(None, ge=1, le=20, description="Phase K: daily failure cap (default 3, v2 auto-off logic)"),
    regime_transition_weekly_fail_limit: Optional[int] = Query(None, ge=1, le=50, description="Phase K: weekly failure cap (default 5)"),
    regime_transition_min_mtf_align: Optional[int] = Query(None, ge=1, le=5, description="Phase K: min MTF alignment count (unused in v1)"),
    # ── Phase L (2026-04-22): S3 Fee-Aware net_ev Gate ──
    # This agent's letter #11 review: 10 criteria + 7 edge cases. paper_mode for 1 week → pass 7 conditions → live.
    s3_gate_enabled: Optional[bool] = Query(None, description="Phase L: S3 Fee-Aware Gate ON/OFF (default OFF)"),
    s3_gate_paper_mode: Optional[bool] = Query(None, description="Phase L: paper mode (True=doesn't skip, virtual counter)"),
    s3_gate_min_net_ev_usdt: Optional[float] = Query(None, ge=-10.0, le=100.0, description="Phase L: net_ev threshold ($), default 0 = break-even"),
    s3_gate_fee_multiplier: Optional[float] = Query(None, ge=1.0, le=5.0, description="Phase L: fee safety-margin multiplier (default 2 = round-trip)"),
    s3_gate_slippage_bps: Optional[float] = Query(None, ge=0.0, le=50.0, description="Phase L: slippage bp (default 5 = 0.05%)"),
    s3_gate_link_multiplier: Optional[float] = Query(None, ge=1.0, le=3.0, description="Phase L: LINK gambler-trait guard multiplier (default 1.3)"),
    # 🪙 Order-book depth size adaptation (2026-06-09 owner "an exchange with no change")
    orderbook_depth_sizing_enabled: Optional[bool] = Query(None, description="🪙 Fit entry size to the order-book capacity — prevents non-fills (default OFF)"),
    orderbook_depth_max_slippage_pct: Optional[float] = Query(None, ge=0.05, le=2.0, description="Count order-book levels within this % as fillable (default 0.3)"),
    orderbook_depth_min_fill_ratio: Optional[float] = Query(None, ge=0.1, le=1.0, description="Skip if capacity/intended < this ratio (default 0.5)"),
    # ── Phase L.1 ─ Fast-Reject reinforcement (ported Harpoon 0-6 → FOCUS) ──
    fast_reject_v2_enabled: Optional[bool] = Query(None, description="Phase L.1: instant cut on a 30s peak of 0% (default OFF)"),
    fast_reject_v2_max_sec: Optional[float] = Query(None, ge=10.0, le=300.0, description="Phase L.1: max inspection seconds"),
    fast_reject_v2_peak_threshold_pct: Optional[float] = Query(None, ge=0.0, le=1.0, description="Phase L.1: peak threshold (%)"),
    fast_reject_v2_pnl_pct: Optional[float] = Query(None, ge=-2.0, le=0.0, description="Phase L.1: pnl threshold (%, negative)"),
    # ── Phase L.2 ─ 30-min re-entry cooldown (ported Harpoon 0-7 → FOCUS) ──
    reentry_cooldown_v2_enabled: Optional[bool] = Query(None, description="Phase L.2: block the same coin+direction for 30 min after an SL (default OFF)"),
    reentry_cooldown_v2_min: Optional[float] = Query(None, ge=5.0, le=240.0, description="Phase L.2: block minutes"),
    # ── Phase L.3 ─ Agreement of 2 PAs (S3 Gate quality gate) ──
    pa_double_confirm_enabled: Optional[bool] = Query(None, description="Phase L.3: on agreement of 2 PAs, S3 Gate net_ev × 1.10 (default OFF)"),
    pa_double_confirm_window_sec: Optional[float] = Query(None, ge=30.0, le=300.0, description="Phase L.3: agreement window (sec)"),
    # ── ★★★ [2026-05-27 owner spirit] Phase 6 — H4/H1 PA + score integration + 5M emergency exit ──
    # α. Phase 6 score integration (Master)
    guard_score_mode_enabled: Optional[bool] = Query(None, description="Phase 6 score-integration master — guard block → conv bonus/penalty (operator 5-27)"),
    guard_score_mode_auto_paper: Optional[bool] = Query(None, description="auto_paper: auto-ON in paper / auto-OFF in LIVE (office protection)"),
    guard_score_threshold: Optional[float] = Query(None, ge=0, le=200, description="Phase 6 entry threshold (default 80)"),
    # ★ [2026-06-12] Trend-alignment multicollinearity cap
    regime_align_cap_enabled: Optional[bool] = Query(None, description="Trend-alignment (Frame+Trend+AltBTC) sum cap — reduces the counter-leg handicap (fair for SHORT) ON/OFF"),
    regime_align_cap: Optional[float] = Query(None, ge=0, le=60, description="Trend-alignment sum clamp ±value (default 15)"),
    combo_f_dedupe_enabled: Optional[bool] = Query(None, description="Remove combo_f direction double-counting (F1 MTF, F2 M5) — fixes the biggest cause of SHORT losses ON/OFF"),
    guard_dir_dedupe_enabled: Optional[bool] = Query(None, description="Remove guard direction double-counting (Frame/Trend/AltBTC/BTC-alignment = overlaps conviction) ON/OFF"),
    # β. PA Completion (Sig + ไส้หลัง) ⭐
    pa_completion_enabled: Optional[bool] = Query(None, description="PA Completion — no entry outside Pat 1/2/3 ⭐ operator core"),
    pa_completion_auto_paper: Optional[bool] = Query(None, description="auto_paper: paper ON / LIVE OFF"),
    pa_completion_huikkang_min_ratio: Optional[float] = Query(None, ge=0.5, le=5, description="Tail body min multiplier (default 1.5)"),
    pa_completion_lookback_bars: Optional[int] = Query(None, ge=2, le=10, description="lookback bars (default 3)"),
    pa_completion_sig_max_ratio: Optional[float] = Query(None, ge=0.3, le=3, description="Sig body max ratio (default 1.0)"),
    # γ. H4 Pulse Only
    h4_pulse_only_enabled: Optional[bool] = Query(None, description="H4 Pulse Only — enter only within N minutes after the H4 close"),
    h4_pulse_auto_paper: Optional[bool] = Query(None, description="auto_paper: paper ON / LIVE OFF"),
    h4_pulse_window_min: Optional[int] = Query(None, ge=5, le=240, description="H4 pulse window minutes (default 60)"),
    # ── [patch v2-A] Pre-Close leading entry ──
    preclose_entry_enabled: Optional[bool] = Query(None, description="[patch v2-A] pre-entry on pattern completion before the H4 close ON/OFF (default OFF)"),
    preclose_min_elapsed_pct: Optional[float] = Query(None, ge=50, le=100, description="[patch v2-A] H4 in-progress bar elapsed-ratio threshold (default 88)"),
    preclose_size_ratio: Optional[float] = Query(None, ge=0.05, le=1.0, description="[patch v2-A] leading-entry size ratio (default 0.5)"),
    preclose_wick_ratio_min: Optional[float] = Query(None, ge=0.5, le=5.0, description="[patch v2-A] pin-bar tail/body ratio (default 1.5)"),
    preclose_body_dir_required: Optional[bool] = Query(None, description="[patch v2-A] use the body-direction + close-position condition (default True)"),
    preclose_max_per_day: Optional[int] = Query(None, ge=0, le=50, description="[patch v2-A] daily leading-entry cap (default 5)"),
    preclose_min_conviction: Optional[float] = Query(None, ge=0, le=200, description="[patch v2-A] leading-eligibility base-conviction floor (default 50)"),
    preclose_topup_enabled: Optional[bool] = Query(None, description="[patch v2-A2] add on close confirmation (second entry) ON/OFF (default OFF)"),
    preclose_topup_min_pnl_pct: Optional[float] = Query(None, ge=-5, le=10, description="[patch v2-A2] add-confirmation pnl floor % (default 0)"),
    preclose_topup_max_chase_pct: Optional[float] = Query(None, ge=0, le=10, description="[patch v2-A2] over-chase cap % (default 1)"),
    preclose_topup_require_candle_dir: Optional[bool] = Query(None, description="[patch v2-A2] require the last closed H4 bar's direction to match (default True)"),
    preclose_topup_grace_min: Optional[float] = Query(None, ge=5, le=240, description="[patch v2-A2] window allowing the add after close (min) (default 60)"),
    # δ. H1 PA Pulse
    h1_pa_pulse_enabled: Optional[bool] = Query(None, description="H1 PA Pulse — also enter on H1 PA besides H4 (operator 5-27 ②)"),
    h1_pa_pulse_auto_paper: Optional[bool] = Query(None, description="auto_paper: paper ON / LIVE OFF"),
    h1_pa_pulse_window_min: Optional[int] = Query(None, ge=5, le=60, description="H1 pulse window minutes (default 15)"),
    h1_pa_pulse_lookback_bars: Optional[int] = Query(None, ge=1, le=5, description="lookback bars (default 2)"),
    h1_pa_pulse_min_confidence: Optional[float] = Query(None, ge=0, le=1, description="min confidence (default 0.5)"),
    h1_pa_pulse_require_day_dir: Optional[bool] = Query(None, description="Force day_direction alignment (default true)"),
    # ε. Anchor Fast-Track
    anchor_fasttrack_enabled: Optional[bool] = Query(None, description="Immediate entry near the anchor — bypasses 5M microtiming"),
    anchor_fasttrack_auto_paper: Optional[bool] = Query(None, description="auto_paper: paper ON / LIVE OFF"),
    anchor_fasttrack_max_proximity: Optional[float] = Query(None, ge=0.05, le=1.0, description="max proximity (default 0.33)"),
    # ζ. Day Box Guard
    day_box_guard_enabled: Optional[bool] = Query(None, description="Day Box — D1 09:00 box ping-pong upper/lower bounds (operator 5-27 ③)"),
    day_box_guard_auto_paper: Optional[bool] = Query(None, description="auto_paper: paper ON / LIVE OFF"),
    day_box_window_hours: Optional[float] = Query(None, ge=1, le=12, description="Box formation time (default 4.0)"),
    day_box_lock_min_hours: Optional[float] = Query(None, ge=0.5, le=12, description="Min time before a lock can be determined (default 3.5)"),
    day_box_max_atr_ratio: Optional[float] = Query(None, ge=0.1, le=3, description="Max ATR ratio (default 0.8)"),
    day_box_min_touches: Optional[int] = Query(None, ge=1, le=10, description="Min touches at both extremes (default 2)"),
    day_box_edge_pct: Optional[float] = Query(None, ge=0.01, le=0.3, description="Edge zone (0~1, default 0.05)"),
    day_box_breakout_pct: Optional[float] = Query(None, ge=0.01, le=2, description="Breakout-determination % (default 0.10)"),
    # η. TF Round TP/SL
    tf_round_tpsl_enabled: Optional[bool] = Query(None, description="TF-Round TP/SL — round ladder off the H4/H1 PA anchor"),
    tf_round_tpsl_auto_paper: Optional[bool] = Query(None, description="auto_paper: paper ON / LIVE OFF"),
    tf_round_anchor_tf: Optional[str] = Query(None, description="anchor TF (60=H1, 240=H4)"),
    tf_round_atr_period: Optional[int] = Query(None, ge=3, le=50, description="ATR period (default 14)"),
    tf_round_tp_atr_mult: Optional[float] = Query(None, ge=0.3, le=5, description="TP1 ATR multiplier (default 1.0)"),
    tf_round_tp2_atr_mult: Optional[float] = Query(None, ge=0.5, le=10, description="TP2 ATR multiplier (default 2.0)"),
    tf_round_sl_ratio: Optional[float] = Query(None, ge=0.1, le=1, description="SL ratio relative to TP1 (default 0.3333)"),
    tf_round_anchor_offset: Optional[int] = Query(None, ge=0, le=5, description="anchor offset (default 0)"),
    tf_round_hold_enabled: Optional[bool] = Query(None, description="Endure mode (short cuts OFF)"),
    # θ. Frame Guard Option B
    frame_guard_option_b_enabled: Optional[bool] = Query(None, description="Frame Guard Option B — 90s silent skip"),
    frame_guard_option_b_auto_paper: Optional[bool] = Query(None, description="auto_paper: paper ON / LIVE OFF"),
    # ι. 5M Emergency Exit ⭐
    exit_5m_emergency_enabled: Optional[bool] = Query(None, description="5M emergency exit — RSI/MACD/BB 'stop here!' ⭐ operator core"),
    exit_5m_emergency_auto_paper: Optional[bool] = Query(None, description="auto_paper: paper ON / LIVE OFF"),
    exit_5m_rsi_overbought: Optional[float] = Query(None, ge=50, le=100, description="LONG-exit RSI threshold (default 70)"),
    exit_5m_rsi_oversold: Optional[float] = Query(None, ge=0, le=50, description="SHORT-exit RSI threshold (default 30)"),
    exit_5m_bb_top_pct: Optional[float] = Query(None, ge=50, le=100, description="LONG-exit BB-position threshold (default 90)"),
    exit_5m_bb_bottom_pct: Optional[float] = Query(None, ge=0, le=50, description="SHORT-exit BB-position threshold (default 10)"),
    exit_5m_min_score: Optional[int] = Query(None, ge=1, le=3, description="Meet ≥ N of the 3 (default 2)"),
    # κ. Guard Score Weights
    guard_score_pa_completion_ok: Optional[float] = Query(None, ge=0, le=100, description="PA-completion bonus (default 30)"),
    guard_score_pa_completion_none: Optional[float] = Query(None, ge=-100, le=0, description="No-PA penalty (default -25)"),
    guard_score_d1_pa_ok: Optional[float] = Query(None, ge=0, le=100, description="D1 PA OK bonus (default 25)"),
    guard_score_d1_pa_none: Optional[float] = Query(None, ge=-100, le=0, description="D1 no-PA penalty (default -15)"),
    guard_score_btc_aligned: Optional[float] = Query(None, ge=0, le=100, description="BTC-alignment bonus (default 15)"),
    guard_score_btc_opposite: Optional[float] = Query(None, ge=-100, le=0, description="BTC-counter penalty (default -15)"),
    guard_score_adx_strong: Optional[float] = Query(None, ge=0, le=100, description="ADX-strong bonus (default 10)"),
    guard_score_adx_weak: Optional[float] = Query(None, ge=-50, le=0, description="ADX-weak penalty (default -5)"),
    guard_score_adx_strong_requires_trend: Optional[bool] = Query(None, description="Exempt the ADX-strong bonus when the structure is SIDEWAYS (score↔chart consistency). default OFF=no live change"),
    naked_sl_guard_enabled: Optional[bool] = Query(None, description="Market-close immediately when the server SL is unconfirmed + price is near SL (prevents a pass-through liquidation). Safety net, default ON"),
    naked_sl_guard_buffer_pct: Optional[float] = Query(None, ge=0, le=5, description="Naked-SL proximity buffer %% (preemptive cut after grace)"),
    server_sl_verify_enabled: Optional[bool] = Query(None, description="On each SYNC, read and compare the exchange's actual stopLoss → re-place if missing/mismatched. Safety net, default ON"),
    guard_score_vol_big_align: Optional[float] = Query(None, ge=0, le=50, description="High-volume bonus (default 10)"),
    guard_score_trend_high_conf: Optional[float] = Query(None, ge=0, le=50, description="Trend high-confidence bonus (default 10)"),
    guard_score_trend_low_conf: Optional[float] = Query(None, ge=-50, le=0, description="Trend low-confidence penalty (default -5)"),
    guard_score_rsi_extreme: Optional[float] = Query(None, ge=0, le=50, description="RSI-extreme bonus (default 10)"),
    guard_score_h4_pulse_in: Optional[float] = Query(None, ge=0, le=100, description="Inside-H4-pulse bonus (default 20)"),
    guard_score_h4_pulse_out: Optional[float] = Query(None, ge=-100, le=0, description="Outside-H4-pulse penalty (default -10)"),
    guard_score_h1_pa_in: Optional[float] = Query(None, ge=0, le=100, description="H1 PA-pass bonus (default 15)"),
    guard_score_h1_pa_out: Optional[float] = Query(None, ge=-50, le=0, description="H1 PA-fail penalty (default -5)"),
    guard_score_frame_aligned: Optional[float] = Query(None, ge=0, le=100, description="Frame-alignment bonus (default 15)"),
    guard_score_frame_neutral: Optional[float] = Query(None, ge=-20, le=50, description="Frame-neutral (default 5)"),
    guard_score_frame_opposite: Optional[float] = Query(None, ge=-100, le=0, description="Frame-opposite penalty (default -20)"),
    guard_score_anchor_close: Optional[float] = Query(None, ge=0, le=100, description="Near-anchor bonus (default 20)"),
    guard_score_anchor_far: Optional[float] = Query(None, ge=-100, le=0, description="Far-from-anchor penalty (default -10)"),
    guard_score_day_box_edge: Optional[float] = Query(None, ge=0, le=50, description="Day Box edge bonus (default 10)"),
    guard_score_day_box_inside: Optional[float] = Query(None, ge=-100, le=0, description="Inside-Day-Box penalty (default -15)"),
    guard_score_microtiming_ok: Optional[float] = Query(None, ge=0, le=50, description="microtiming-OK bonus (default 10)"),
    guard_score_microtiming_no: Optional[float] = Query(None, ge=-50, le=0, description="microtiming-fail penalty (default -5)"),
    guard_score_raw_body_align: Optional[float] = Query(None, ge=0, le=50, description="raw_body-alignment bonus (default 5)"),
    guard_score_raw_body_against: Optional[float] = Query(None, ge=-100, le=0, description="raw_body-opposite penalty (default -15)"),
    guard_score_momentum_deriv_align: Optional[float] = Query(None, ge=0, le=50, description="momentum-match bonus (default 5)"),
    guard_score_momentum_deriv_against: Optional[float] = Query(None, ge=-50, le=0, description="momentum-opposite penalty (default -10)"),
    # ── ★ [2026-06-03 owner] D1 trend weighting + gap-check gate ──
    d1_trend_weight: Optional[float] = Query(None, ge=0, le=3, description="D1 (daily) trend weight (default 1.0, ×6=max)"),
    cr_speed_sign_guard_enabled: Optional[bool] = Query(None, description="Fix A — if the cr direction (count) is UP but the actual change is negative, neutralize (blocks a fake UP bonus). BEAT incident. default OFF"),
    cr_blowoff_extreme_guard_enabled: Optional[bool] = Query(None, description="Fix B — extreme spike/dump (blowoff)=late → neutralize the cr direction. Blocks the D1 +103% afterimage. default OFF"),
    cr_blowoff_extreme_ratio: Optional[float] = Query(None, ge=1, le=20, description="blowoff threshold speed/ATR ratio (default 4.0, lower=judges late more often)"),
    cr_trend_agree_guard_enabled: Optional[bool] = Query(None, description="Fix C — if the 5-candle direction opposes the longer trend (lookback), neutralize. Blocks mistaking ripples for a trend. default OFF"),
    cr_trend_agree_lookback: Optional[int] = Query(None, ge=6, le=120, description="Fix C candles for determining the larger trend (default 20)"),
    gap_check_enabled: Optional[bool] = Query(None, description="Gap-check gate — verify the distance to the TF×N-bar top/bottom before entry ON/OFF"),
    gap_check_tf: Optional[str] = Query(None, description="Gap-check TF (5 / 15 / 30 / 60)"),
    gap_check_lookback_bars: Optional[int] = Query(None, ge=6, le=48, description="Gap-check lookback bars (default 12, 12×15M=3h)"),
    gap_check_min_pct: Optional[float] = Query(None, ge=0, le=5, description="Min gap % (0=OFF, 0.3 recommended)"),
    gap_check_atr_adaptive_enabled: Optional[bool] = Query(None, description="Gap ATR adaptation — high-range coins need a larger gap↑ (blocks top-chasing, 2026-06-07)"),
    gap_check_atr_mult: Optional[float] = Query(None, ge=0, le=3, description="Required gap = ATR% × this (default 0.7, larger=enters only lower)"),
    gap_check_atr_cap_pct: Optional[float] = Query(None, ge=0.1, le=5, description="ATR-adaptive required-gap cap % (default 1.5)"),
    gap_proximity_exit_enabled: Optional[bool] = Query(None, description="Gap-approach exit — preemptive exit near the top/bottom ON/OFF"),
    gap_proximity_exit_tf: Optional[str] = Query(None, description="Gap-approach exit TF (5 / 15 / 30 / 60)"),
    gap_proximity_exit_pct: Optional[float] = Query(None, ge=0.1, le=2, description="Approach threshold % (exit when within this, default 0.2)"),
    # ── ★ [2026-06-15 solution B·C] Observation toggles (does not touch entry logic, default OFF) ──
    gate_ledger_enabled: Optional[bool] = Query(None, description="B: tally gate pass/reject ('why was it silent'). Observation only, no entry impact"),
    dual_observe_auto_off_weak: Optional[bool] = Query(None, description="C: auto-OFF dual observe on weak servers (RAM ≤ threshold) (lowers load). Entry unchanged, no effect on strong servers"),
):
    """Update FOCUS configuration (partial update)."""
    fm = _get_fm(request)
    patch = {}
    for k, v in {
        "budget_usdt": budget_usdt, "leverage": leverage, "max_positions": max_positions,
        "direction_mode": direction_mode,
        "risk_pct": risk_pct, "max_daily_plans": max_daily_plans, "max_daily_sl": max_daily_sl,
        "cooldown_sec": cooldown_sec, "scan_interval_sec": scan_interval_sec,
        "cycle_tp1_mult": cycle_tp1_mult, "cycle_tp2_mult": cycle_tp2_mult,
        "cycle_sl_mult": cycle_sl_mult, "partial_exit_pct": partial_exit_pct,
        "trailing_pct": trailing_pct,
        "dynamic_trailing": dynamic_trailing,
        "breakeven_trigger_pct": breakeven_trigger_pct,
        "trailing_preserve_pct": trailing_preserve_pct,
        "trailing_small_profit_preserve_pct": trailing_small_profit_preserve_pct,
        "trailing_accel_pct": trailing_accel_pct,
        "adx_filter_enabled": adx_filter_enabled,
        "min_adx_entry": min_adx_entry,
        "dormant_adx_threshold": dormant_adx_threshold,
        "min_conviction": min_conviction,
        "scanner_entry": scanner_entry,
        "scanner_min_adx": scanner_min_adx,
        "scanner_min_conviction": scanner_min_conviction,
        "scanner_max_exposure_pct": scanner_max_exposure_pct,
        "scanner_m30_primary_conflict_penalty": scanner_m30_primary_conflict_penalty,
        "scanner_m30_direction_conflict_penalty": scanner_m30_direction_conflict_penalty,
        "entry_mode": entry_mode,
        "entry_guard_set": entry_guard_set,
        "exit_guard_set": exit_guard_set,
        "smart_manual_entry_enabled": smart_manual_entry_enabled,
        "smart_manual_entry_default_timeout_sec": smart_manual_entry_default_timeout_sec,
        "slot_auto_expand_enabled": slot_auto_expand_enabled,
        "slot_auto_expand_lock_hours": slot_auto_expand_lock_hours,
        "slot_auto_expand_min_conviction": slot_auto_expand_min_conviction,
        "slot_auto_expand_max_extra": slot_auto_expand_max_extra,
        "slot_auto_expand_size_ratio": slot_auto_expand_size_ratio,
        "market_consensus_exit_enabled": market_consensus_exit_enabled,
        "market_consensus_threshold_pct": market_consensus_threshold_pct,
        "market_consensus_duration_min": market_consensus_duration_min,
        "market_consensus_min_hold_min": market_consensus_min_hold_min,
        "market_consensus_min_pnl_pct": market_consensus_min_pnl_pct,
        "reverse_conv_threshold": reverse_conv_threshold,
        "reverse_adx_max": reverse_adx_max,
        "charge_exit_enabled": charge_exit_enabled,
        "charge_exit_min_pnl_pct": charge_exit_min_pnl_pct,
        "charge_exit_conv_delta": charge_exit_conv_delta,
        "max_same_direction": max_same_direction,
        "coin_loss_cap_enabled": coin_loss_cap_enabled,
        "coin_loss_cap_amount": coin_loss_cap_amount,
        "coin_loss_cap_window_hours": coin_loss_cap_window_hours,
        # ★ Per-Coin Size Cap (2026-05-08 owner decision)
        "per_coin_size_cap_enabled": per_coin_size_cap_enabled,
        "per_coin_size_cap_pct": per_coin_size_cap_pct,
        # ★ Conviction Override Slot (2026-05-10 owner decision)
        "override_slot_enabled": override_slot_enabled,
        "override_min_conviction": override_min_conviction,
        "override_locked_slot_min_hours": override_locked_slot_min_hours,
        "override_size_cap_pct": override_size_cap_pct,
        "override_max_sl_distance_pct": override_max_sl_distance_pct,
        "override_hard_roe_cut_pct": override_hard_roe_cut_pct,
        # Momentum Reversal (Phase 4 hard penalty 18)
        "momentum_reversal_enabled": momentum_reversal_enabled,
        "momentum_reversal_strong_atr": momentum_reversal_strong_atr,
        "momentum_reversal_medium_atr": momentum_reversal_medium_atr,
        "momentum_reversal_strong_weight": momentum_reversal_strong_weight,
        "momentum_reversal_medium_weight": momentum_reversal_medium_weight,
        "momentum_reversal_lookback_bars": momentum_reversal_lookback_bars,
        "coin_repeat_brake_enabled": coin_repeat_brake_enabled,
        "coin_repeat_free_count": coin_repeat_free_count,
        "coin_repeat_cooldown_base": coin_repeat_cooldown_base,
        # ★ BE Stall Exit (2026-05-14 owner — exposed in UI)
        "be_stall_exit_enabled": be_stall_exit_enabled,
        "be_stall_exit_sec": be_stall_exit_sec,
        "be_stall_intelligent_enabled": be_stall_intelligent_enabled,
        "be_stall_intelligent_rsi_strong": be_stall_intelligent_rsi_strong,
        "be_stall_intelligent_rsi_weak": be_stall_intelligent_rsi_weak,
        # ★ Pre-BE Stall Exit (2026-04-23 owner instruction)
        "pre_be_stall_exit_mode": pre_be_stall_exit_mode,
        "pre_be_stall_min_profit_pct": pre_be_stall_min_profit_pct,
        "pre_be_stall_sec": pre_be_stall_sec,
        "pre_be_stall_volatility_threshold_pct": pre_be_stall_volatility_threshold_pct,
        "pre_be_stall_max_since_peak_sec": pre_be_stall_max_since_peak_sec,
        # 🐢 Pre-BE loss guard (2026-06-09 owner "right now")
        "pre_be_loss_guard_enabled": pre_be_loss_guard_enabled,
        "pre_be_loss_guard_peak_max_pct": pre_be_loss_guard_peak_max_pct,
        "pre_be_loss_guard_trigger_loss_pct": pre_be_loss_guard_trigger_loss_pct,
        "pre_be_loss_guard_min_hold_sec": pre_be_loss_guard_min_hold_sec,
        "pre_be_loss_guard_max_age_sec": pre_be_loss_guard_max_age_sec,
        # ★ Reverse Drift Exit (2026-05-16 owner instruction)
        "reverse_drift_exit_enabled": reverse_drift_exit_enabled,
        "reverse_drift_peak_min_pct": reverse_drift_peak_min_pct,
        "reverse_drift_peak_max_pct": reverse_drift_peak_max_pct,
        "reverse_drift_min_since_peak_sec": reverse_drift_min_since_peak_sec,
        "reverse_drift_max_since_peak_sec": reverse_drift_max_since_peak_sec,
        "reverse_drift_pct": reverse_drift_pct,
        "reverse_drift_atr_adaptive_enabled": reverse_drift_atr_adaptive_enabled,
        "reverse_drift_atr_multiplier": reverse_drift_atr_multiplier,
        "reverse_drift_atr_cap_pct": reverse_drift_atr_cap_pct,
        # ★ Late-chase block (Overextension) — 2026-06-07 owner
        "overextension_enabled": overextension_enabled,
        "overextension_range_pos_pct": overextension_range_pos_pct,
        "overextension_min_move_pct": overextension_min_move_pct,
        "overextension_penalty": overextension_penalty,
        "overextension_adx_exempt": overextension_adx_exempt,
        "blowoff_filter_enabled": blowoff_filter_enabled,
        "blowoff_move_pct": blowoff_move_pct,
        "blowoff_penalty": blowoff_penalty,
        "blowoff_extreme_pct": blowoff_extreme_pct,
        "blowoff_max_penalty": blowoff_max_penalty,
        "blowoff_chase_only": blowoff_chase_only,
        # 🎯 Inflection setup score (2026-06-12)
        "inflection_setup_enabled": inflection_setup_enabled,
        "inflection_setup_weight": inflection_setup_weight,
        "inflection_setup_cap": inflection_setup_cap,
        "inflection_setup_base": inflection_setup_base,
        "inflection_setup_slope_scale": inflection_setup_slope_scale,
        # 🎣 Retest setup score (2026-06-12)
        "retest_setup_enabled": retest_setup_enabled,
        "retest_setup_weight": retest_setup_weight,
        "retest_setup_turn_bonus": retest_setup_turn_bonus,
        "retest_retr_lo": retest_retr_lo,
        "retest_retr_hi": retest_retr_hi,
        # 🌋 Volatility-awakening SL adaptation (2026-06-11)
        "awaken_sl_enabled": awaken_sl_enabled,
        "awaken_sl_mode": awaken_sl_mode,
        "awaken_atr_ratio": awaken_atr_ratio,
        "awaken_atr_lookback": awaken_atr_lookback,
        "awaken_max_sl_mult": awaken_max_sl_mult,
        "awaken_require_day_align": awaken_require_day_align,
        "awaken_swing_lookback": awaken_swing_lookback,
        "awaken_atr_buffer": awaken_atr_buffer,
        # ② Late ceiling penalty (2026-06-09)
        "conviction_ceiling_enabled": conviction_ceiling_enabled,
        "conviction_ceiling_start": conviction_ceiling_start,
        "conviction_ceiling_target": conviction_ceiling_target,
        "conviction_ceiling_adx_exempt": conviction_ceiling_adx_exempt,
        # ★ Headroom penalty (2026-06-09)
        "headroom_penalty_enabled": headroom_penalty_enabled,
        "headroom_sr_penalty": headroom_sr_penalty,
        "headroom_sr_near_pct": headroom_sr_near_pct,
        "headroom_rsi_penalty": headroom_rsi_penalty,
        "headroom_rsi_overbought": headroom_rsi_overbought,
        "headroom_rsi_oversold": headroom_rsi_oversold,
        "headroom_bb_penalty": headroom_bb_penalty,
        "headroom_bb_hi_pctb": headroom_bb_hi_pctb,
        "headroom_bb_lo_pctb": headroom_bb_lo_pctb,
        # 🌊 Macro-down active SHORT entry, stage 2 (2026-06-11)
        "macro_short_timing_enabled": macro_short_timing_enabled,
        "macro_short_timing_delta": macro_short_timing_delta,
        "macro_short_timing_min_signals": macro_short_timing_min_signals,
        "macro_short_timing_bounce_pct": macro_short_timing_bounce_pct,
        "macro_short_timing_lookback": macro_short_timing_lookback,
        # ★ Regime-counter holding exit P3 (fix for missing router wiring 2026-06-07)
        "macro_exit_enabled": macro_exit_enabled,
        "macro_exit_breadth_min": macro_exit_breadth_min,
        "macro_exit_sl_cushion_pct": macro_exit_sl_cushion_pct,
        "macro_exit_strong_coin_exempt": macro_exit_strong_coin_exempt,
        "macro_exit_exempt_min_roe": macro_exit_exempt_min_roe,
        # ★ Batch fix for missing router wiring (2026-06-07) — 12 fields
        "bb_block_trend_bypass_adx": bb_block_trend_bypass_adx,
        "bb_trend_bypass_require_di": bb_trend_bypass_require_di,
        "bb_trend_bypass_macd_min": bb_trend_bypass_macd_min,
        "final_30m15m_bypass_conviction": final_30m15m_bypass_conviction,
        "final_30m15m_bypass_include_regime": final_30m15m_bypass_include_regime,
        "final_d1_bypass_conviction": final_d1_bypass_conviction,
        "final_d1_recent5_override_enabled": final_d1_recent5_override_enabled,
        "final_d1_recent5_drop_pct": final_d1_recent5_drop_pct,
        "d1_reality_demote_enabled": d1_reality_demote_enabled,
        "d1_reality_demote_drop_pct": d1_reality_demote_drop_pct,
        "guard_score_total_cap_enabled": guard_score_total_cap_enabled,
        "guard_score_total_cap": guard_score_total_cap,
        "conviction_ceiling_post_guards": conviction_ceiling_post_guards,
        "final_bypass_use_base": final_bypass_use_base,
        "final_5m_simple_check_enabled": final_5m_simple_check_enabled,
        "final_d1_alignment_check_enabled": final_d1_alignment_check_enabled,
        "final_align_regime_override_enabled": final_align_regime_override_enabled,
        "final_5m_simple_min_score": final_5m_simple_min_score,
        "final_5m_bb_trend_bypass_enabled": final_5m_bb_trend_bypass_enabled,
        "macro_compass_enabled": macro_compass_enabled,
        "macro_recovering_conv_delta": macro_recovering_conv_delta,
        "macro_recovering_require_di_adx": macro_recovering_require_di_adx,
        "macro_recovering_min_adx": macro_recovering_min_adx,
        "micro_1m_body_min_pct": micro_1m_body_min_pct,
        "multi_be_lock_atr_adaptive_enabled": multi_be_lock_atr_adaptive_enabled,
        "multi_be_lock_atr_min_stage1_trigger_pct": multi_be_lock_atr_min_stage1_trigger_pct,
        "multi_be_lock_atr_max_stage1_trigger_pct": multi_be_lock_atr_max_stage1_trigger_pct,
        # ★ [2026-05-18 owner vision #6] Entry Grace Period + Market Bias Grace Exit + News Grace Exit
        "entry_grace_period_sec": entry_grace_period_sec,
        "market_bias_grace_exit_enabled": market_bias_grace_exit_enabled,
        "news_grace_exit_enabled": news_grace_exit_enabled,
        "news_grace_exit_threshold": news_grace_exit_threshold,
        # ★★★★ [2026-05-18 owner vision #6 option B] Time-independent OR condition
        "exit_consensus_enabled": exit_consensus_enabled,
        "exit_consensus_news_threshold": exit_consensus_news_threshold,
        # ★ Long Hold Timeout (3-tier, 2026-04-25)
        "long_hold_timeout_enabled": long_hold_timeout_enabled,
        "long_hold_timeout_tier1_min": long_hold_timeout_tier1_min,
        "long_hold_timeout_tier1_peak_pct": long_hold_timeout_tier1_peak_pct,
        "long_hold_timeout_tier2_min": long_hold_timeout_tier2_min,
        "long_hold_timeout_tier2_peak_pct": long_hold_timeout_tier2_peak_pct,
        "long_hold_timeout_tier3_min": long_hold_timeout_tier3_min,
        "long_hold_timeout_tier3_peak_pct": long_hold_timeout_tier3_peak_pct,
        # ★ Entry Expectation (2026-05-14 owner — entry-expectation mechanism)
        "entry_expectation_enabled": entry_expectation_enabled,
        "expectation_progress_exit_enabled": expectation_progress_exit_enabled,
        "expectation_progress_t1_min": expectation_progress_t1_min,
        "expectation_progress_t1_pct": expectation_progress_t1_pct,
        "expectation_progress_t2_min": expectation_progress_t2_min,
        "expectation_progress_t2_pct": expectation_progress_t2_pct,
        # ★ Negative-progress instant cut (2026-05-15 owner)
        "expectation_progress_neg_cut_enabled": expectation_progress_neg_cut_enabled,
        "expectation_progress_neg_cut_pct": expectation_progress_neg_cut_pct,
        "expectation_progress_neg_cut_min": expectation_progress_neg_cut_min,
        # ★ Entry Quality Gates (2026-05-15 owner)
        "entry_expectation_gate_enabled": entry_expectation_gate_enabled,
        "entry_expectation_min_rr": entry_expectation_min_rr,
        "entry_expectation_min_reward_pct": entry_expectation_min_reward_pct,
        "entry_expectation_max_risk_pct": entry_expectation_max_risk_pct,
        # ★ Macro regime direction gate (2026-06-02 owner)
        "breadth_strong_n": breadth_strong_n,
        "breadth_mid_n": breadth_mid_n,
        "breadth_aligned_strong": breadth_aligned_strong,
        "breadth_aligned_mid": breadth_aligned_mid,
        "breadth_counter_strong": breadth_counter_strong,
        "breadth_counter_mid": breadth_counter_mid,
        "regime_counter_strong_cap_enabled": regime_counter_strong_cap_enabled,
        "regime_counter_strong_cap": regime_counter_strong_cap,
        "regime_short_release_enabled": regime_short_release_enabled,
        "regime_short_release_n": regime_short_release_n,
        "coin_decouple_enabled": coin_decouple_enabled,
        "coin_decouple_short_release": coin_decouple_short_release,
        "coin_decouple_long_penalty": coin_decouple_long_penalty,
        "coin_decouple_min_strength": coin_decouple_min_strength,
        "coin_decouple_btc_cache_sec": coin_decouple_btc_cache_sec,
        "mom_decouple_enabled": mom_decouple_enabled,
        "mom_decouple_weight": mom_decouple_weight,
        "mom_decouple_cap": mom_decouple_cap,
        "mom_decouple_base": mom_decouple_base,
        "mom_decouple_up_thr": mom_decouple_up_thr,
        "mom_decouple_div_thr": mom_decouple_div_thr,
        "mom_decouple_pos_hi": mom_decouple_pos_hi,
        "mom_decouple_pos_lo": mom_decouple_pos_lo,
        "mom_decouple_btc_cache_sec": mom_decouple_btc_cache_sec,
        "reversal_score": reversal_score,
        # ★ TF trend weighting (2026-06-03 owner)
        "h4_trend_weight": h4_trend_weight,
        "h1_trend_weight": h1_trend_weight,
        "m30_trend_weight": m30_trend_weight,
        "m15_trend_weight": m15_trend_weight,
        "m5_trend_weight": m5_trend_weight,
        "breadth_dir_chg1h_pct": breadth_dir_chg1h_pct,
        "breadth_dir_ema_pct": breadth_dir_ema_pct,
        "entry_volatility_gate_enabled": entry_volatility_gate_enabled,
        "entry_volatility_lookback_tf": entry_volatility_lookback_tf,
        "entry_volatility_lookback_bars": entry_volatility_lookback_bars,
        "entry_volatility_min_reach_ratio": entry_volatility_min_reach_ratio,
        "entry_flip_require_alignment": entry_flip_require_alignment,
        # ★ Long Hold Persistence (2026-04-26)
        "trend_reversal_enabled": trend_reversal_enabled,
        "bb_macd_sw_enabled": bb_macd_sw_enabled,
        "bb_macd_sw_min_hold_hours": bb_macd_sw_min_hold_hours,
        "bb_macd_sw_pnl_low": bb_macd_sw_pnl_low,
        "bb_macd_sw_pnl_high": bb_macd_sw_pnl_high,
        "caution_sideways_profit_secure_enabled": caution_sideways_profit_secure_enabled,
        "caution_min_hold_sec": caution_min_hold_sec,
        "caution_fee_rate": caution_fee_rate,
        "caution_min_profit_multiplier": caution_min_profit_multiplier,
        "quick_tp_enabled": quick_tp_enabled,
        "quick_tp_min_hold_hours": quick_tp_min_hold_hours,
        "quick_tp_min_pnl_pct": quick_tp_min_pnl_pct,
        "btc_crash_threshold_pct": btc_crash_threshold_pct,
        "btc_emergency_pause_enabled": btc_emergency_pause_enabled,
        "btc_emergency_pause_threshold_pct": btc_emergency_pause_threshold_pct,
        "btc_emergency_pause_window_min": btc_emergency_pause_window_min,
        "btc_emergency_mode": btc_emergency_mode,
        "btc_emergency_aggressive_entry": btc_emergency_aggressive_entry,
        "btc_emergency_aligned_duration_min": btc_emergency_aligned_duration_min,
        "winners_add_enabled": winners_add_enabled,
        "winners_add_capital_threshold_pct": winners_add_capital_threshold_pct,
        "winners_add_min_pnl_pct": winners_add_min_pnl_pct,
        "winners_add_max_per_event": winners_add_max_per_event,
        "winners_add_max_pct_per_coin": winners_add_max_pct_per_coin,
        "winners_add_cooldown_sec": winners_add_cooldown_sec,
        "min_sl_pct": min_sl_pct,
        "max_sl_distance_pct": max_sl_distance_pct,
        "max_atr_pct": max_atr_pct,
        "cycle_min_rr": cycle_min_rr,
        # ★ Min TP fee-guard (2026-05-15 owner)
        "min_tp_distance_enabled": min_tp_distance_enabled,
        "min_tp_distance_pct": min_tp_distance_pct,
        # ★ 5m Microtiming Gate (2026-05-16 owner)
        "microtiming_5m_enabled": microtiming_5m_enabled,
        "microtiming_5m_min_score": microtiming_5m_min_score,
        "microtiming_5m_defer_sec": microtiming_5m_defer_sec,
        "microtiming_5m_max_defers": microtiming_5m_max_defers,
        "microtiming_5m_rsi_long_threshold": microtiming_5m_rsi_long_threshold,
        "microtiming_5m_rsi_short_threshold": microtiming_5m_rsi_short_threshold,
        "microtiming_5m_bb_low_pct": microtiming_5m_bb_low_pct,
        "microtiming_5m_bb_recover_pct": microtiming_5m_bb_recover_pct,
        "microtiming_5m_phase_k_exempt": microtiming_5m_phase_k_exempt,
        # ★ DrawdownShield base (2026-05-16 owner)
        "drawdown_shield_use_cash_only": drawdown_shield_use_cash_only,
        "drawdown_shield_caution_pct": drawdown_shield_caution_pct,
        "drawdown_shield_defend_pct": drawdown_shield_defend_pct,
        "drawdown_shield_crisis_pct": drawdown_shield_crisis_pct,
        "drawdown_shield_caution_usd": drawdown_shield_caution_usd,
        "drawdown_shield_defend_usd": drawdown_shield_defend_usd,
        "drawdown_shield_crisis_usd": drawdown_shield_crisis_usd,
        "drawdown_shield_caution_pen": drawdown_shield_caution_pen,
        "drawdown_shield_defend_pen": drawdown_shield_defend_pen,
        "drawdown_shield_crisis_pen": drawdown_shield_crisis_pen,
        # ★ [2026-05-16 owner] Same-coin Flip Cooldown + 5m Raw Body Guard + Imminent Flip
        "same_coin_flip_cooldown_enabled": same_coin_flip_cooldown_enabled,
        "same_coin_flip_cooldown_min": same_coin_flip_cooldown_min,
        "micro_1m_check_enabled": micro_1m_check_enabled,
        "micro_1m_candle_check": micro_1m_candle_check,
        "micro_1m_candle_trend_exempt_adx": micro_1m_candle_trend_exempt_adx,
        "micro_1m_volume_check": micro_1m_volume_check,
        "micro_1m_rsi_check": micro_1m_rsi_check,
        "micro_1m_rsi_long_max": micro_1m_rsi_long_max,
        "micro_1m_rsi_short_min": micro_1m_rsi_short_min,
        "micro_1m_vol_decline_bars": micro_1m_vol_decline_bars,
        "raw_body_guard_enabled": raw_body_guard_enabled,
        "raw_body_guard_lookback": raw_body_guard_lookback,
        "raw_body_guard_min_net_pct": raw_body_guard_min_net_pct,
        "momentum_deriv_guard_enabled": momentum_deriv_guard_enabled,
        "momentum_deriv_guard_tf": momentum_deriv_guard_tf,
        "momentum_deriv_guard_lookback": momentum_deriv_guard_lookback,
        "momentum_deriv_guard_rsi_min_slope": momentum_deriv_guard_rsi_min_slope,
        "momentum_deriv_guard_macd_min_slope": momentum_deriv_guard_macd_min_slope,
        "momentum_deriv_guard_require_both": momentum_deriv_guard_require_both,
        "mtf_momentum_align_enabled": mtf_momentum_align_enabled,
        "mtf_momentum_align_tfs": mtf_momentum_align_tfs,
        "mtf_momentum_align_lookback": mtf_momentum_align_lookback,
        "mtf_momentum_align_min_aligned": mtf_momentum_align_min_aligned,
        "mtf_momentum_align_rsi_slope_thr": mtf_momentum_align_rsi_slope_thr,
        "mtf_momentum_align_use_macd": mtf_momentum_align_use_macd,
        "cfid_enabled": cfid_enabled,
        "cfid_tf": cfid_tf,
        "cfid_ema_gap_thr_pct": cfid_ema_gap_thr_pct,
        "cfid_volume_spike_ratio": cfid_volume_spike_ratio,
        "cfid_adx_change_min": cfid_adx_change_min,
        "cfid_lookback": cfid_lookback,
        "cfid_bypass_momentum_deriv": cfid_bypass_momentum_deriv,
        "cfid_bypass_mtf_align": cfid_bypass_mtf_align,
        # ★ [2026-05-18 owner vision #5] Leading Entry
        "leading_entry_mode": leading_entry_mode,
        "cfid_leading_min_strength": cfid_leading_min_strength,
        "cfid_leading_size_pct": cfid_leading_size_pct,
        "cfid_leading_bypass_microtiming": cfid_leading_bypass_microtiming,
        "cfid_leading_bypass_bb_regime": cfid_leading_bypass_bb_regime,
        "pattern_leading_size_pct": pattern_leading_size_pct,
        "pattern_leading_min_5step_score": pattern_leading_min_5step_score,
        "pattern_leading_max_sr_pct": pattern_leading_max_sr_pct,
        "pattern_leading_min_mtf_align": pattern_leading_min_mtf_align,
        "pattern_leading_bypass_microtiming": pattern_leading_bypass_microtiming,
        "pattern_leading_bypass_bb_regime": pattern_leading_bypass_bb_regime,
        # ★ [2026-05-19 Phase 6 Step 2 B-Full] Combinatorial Weighting
        "phase6_combo_a_bonus": phase6_combo_a_bonus,
        "phase6_combo_a_sr_min": phase6_combo_a_sr_min,
        "phase6_combo_a_mtf_min": phase6_combo_a_mtf_min,
        "phase6_combo_b_bonus": phase6_combo_b_bonus,
        "phase6_combo_b_strength_min": phase6_combo_b_strength_min,
        "phase6_combo_c_bonus": phase6_combo_c_bonus,
        "phase6_combo_c_5step_min": phase6_combo_c_5step_min,
        "phase6_combo_d_bonus": phase6_combo_d_bonus,
        "phase6_combo_d_news_abs_min": phase6_combo_d_news_abs_min,
        # ★ [2026-05-19] BB block-guard thresholds
        "bb_block_threshold_pct": bb_block_threshold_pct,
        "bb_penalty_threshold_pct": bb_penalty_threshold_pct,
        "bb_penalty_amount": bb_penalty_amount,
        "coin_state_machine_enabled": coin_state_machine_enabled,
        "coin_state_apply_conv_adjust": coin_state_apply_conv_adjust,
        "coin_state_accel_conv_adj": coin_state_accel_conv_adj,
        "coin_state_steady_conv_adj": coin_state_steady_conv_adj,
        "coin_state_decel_conv_adj": coin_state_decel_conv_adj,
        "coin_state_flip_imminent_conv_adj": coin_state_flip_imminent_conv_adj,
        "tight_trail_after_be_enabled": tight_trail_after_be_enabled,
        "tight_trail_max_slippage_pct": tight_trail_max_slippage_pct,
        "tight_trail_min_peak_pct": tight_trail_min_peak_pct,
        "tight_trail_atr_adaptive_enabled": tight_trail_atr_adaptive_enabled,
        "tight_trail_atr_tf": tight_trail_atr_tf,
        "tight_trail_atr_period": tight_trail_atr_period,
        "tight_trail_atr_multiplier": tight_trail_atr_multiplier,
        "tight_trail_atr_cap_pct": tight_trail_atr_cap_pct,
        "trend_adaptive_exit_enabled": trend_adaptive_exit_enabled,
        "trend_adaptive_exit_adx_strong": trend_adaptive_exit_adx_strong,
        "trend_adaptive_exit_adx_weak": trend_adaptive_exit_adx_weak,
        "trend_adaptive_exit_runner_factor": trend_adaptive_exit_runner_factor,
        "trend_adaptive_exit_chop_factor": trend_adaptive_exit_chop_factor,
        "trend_adaptive_exit_adx_cache_sec": trend_adaptive_exit_adx_cache_sec,
        "imminent_flip_enabled": imminent_flip_enabled,
        "imminent_flip_ema_gap_pct": imminent_flip_ema_gap_pct,
        "imminent_flip_use_30m": imminent_flip_use_30m,
        "imminent_flip_adx_rise_min": imminent_flip_adx_rise_min,
        "imminent_flip_gap_lookback": imminent_flip_gap_lookback,
        # ★ Hard ROE Cap (2026-04-25)
        "hard_roe_cap_enabled": hard_roe_cap_enabled,
        "hard_roe_cap_roe_pct": hard_roe_cap_roe_pct,
        # ★ Leverage Tier (ATR-based tiering, 2026-04-25)
        "leverage_tier_enabled": leverage_tier_enabled,
        "leverage_tier_atr_low_pct": leverage_tier_atr_low_pct,
        "leverage_tier_low": leverage_tier_low,
        "leverage_tier_atr_high_pct": leverage_tier_atr_high_pct,
        "leverage_tier_high": leverage_tier_high,
        "thesis_invalidation_enabled": thesis_invalidation_enabled,
        "thesis_invalidation_min_hold_h": thesis_invalidation_min_hold_h,
        "thesis_invalidation_max_peak_pct": thesis_invalidation_max_peak_pct,
        # Morning Shield / Guard
        "morning_shield_enabled": morning_shield_enabled,
        "morning_guard_enabled": morning_guard_enabled,
        "morning_shield_lock_pct": morning_shield_lock_pct,
        "morning_guard_conviction_boost": morning_guard_conviction_boost,
        "morning_guard_end_hour_kst": morning_guard_end_hour_kst,
        # Event Shield (economic-event shield)
        "event_shield_enabled": event_shield_enabled,
        "event_shield_times_kst": event_shield_times_kst,
        "event_shield_window_min": event_shield_window_min,
        "event_shield_lead_min": event_shield_lead_min,
        "event_shield_lock_pct": event_shield_lock_pct,
        "event_shield_auto_fetch": event_shield_auto_fetch,
        # Auto Take-Profit (trailing harvest) / Stop-Loss (2026-06-08 owner, harvest winners)
        "auto_tp_enabled": auto_tp_enabled,
        "auto_tp_usdt": auto_tp_usdt,
        "auto_tp_peak_giveback_pct": auto_tp_peak_giveback_pct,
        "auto_sl_pct_enabled": auto_sl_pct_enabled,
        "auto_sl_pct": auto_sl_pct,
        "dual_direction_observe": dual_direction_observe,
        "dual_direction_enabled": dual_direction_enabled,
        # Erosion Guard
        "erosion_guard_enabled": erosion_guard_enabled,
        "erosion_guard_peak_pct": erosion_guard_peak_pct,
        "erosion_guard_ratio": erosion_guard_ratio,
        # SL Dodge
        "sl_dodge_enabled": sl_dodge_enabled,
        "sl_dodge_proximity_pct": sl_dodge_proximity_pct,
        "sl_dodge_retreat_pct": sl_dodge_retreat_pct,
        "sl_dodge_max_count": sl_dodge_max_count,
        "sl_dodge_max_total_pct": sl_dodge_max_total_pct,
        # SL Decay
        "sl_decay_enabled": sl_decay_enabled,
        "sl_decay_2h_ratio": sl_decay_2h_ratio,
        "sl_decay_3h_ratio": sl_decay_3h_ratio,
        # Fast-Reject
        "fast_reject_enabled": fast_reject_enabled,
        "fast_reject_min_sec": fast_reject_min_sec,
        "fast_reject_max_sec": fast_reject_max_sec,
        "fast_reject_peak_threshold_pct": fast_reject_peak_threshold_pct,
        "fast_reject_trigger_pnl_pct": fast_reject_trigger_pnl_pct,
        # Entry Quality Filter
        "entry_quality_enabled": entry_quality_enabled,
        "eq_momentum_enabled": eq_momentum_enabled,
        "eq_momentum_count": eq_momentum_count,
        "eq_momentum_min_agree": eq_momentum_min_agree,
        "eq_bb_enabled": eq_bb_enabled,
        "eq_bb_upper_pct": eq_bb_upper_pct,
        "eq_bb_lower_pct": eq_bb_lower_pct,
        "eq_nbar_enabled": eq_nbar_enabled,
        "eq_nbar_count": eq_nbar_count,
        "eq_nbar_min_ratio": eq_nbar_min_ratio,
        # Advanced
        "rr_ratio": rr_ratio,
        "adaptive_cooldown": adaptive_cooldown,
        "emergency_tp_tiers": emergency_tp_tiers,
        "coin_repeat_window_hours": coin_repeat_window_hours,
        # Manual Exit Penalty
        "manual_exit_penalty_enabled": manual_exit_penalty_enabled,
        "manual_exit_penalty_hours": manual_exit_penalty_hours,
        "phase3_context_bonus_enabled": phase3_context_bonus_enabled,
        # [2026-05-19] 124 advanced
        "primary_tf": primary_tf,
        "entry_tf": entry_tf,
        "post_trade_pause_enabled": post_trade_pause_enabled,
        "post_trade_pause_profit_sec": post_trade_pause_profit_sec,
        "post_trade_pause_loss_sec": post_trade_pause_loss_sec,
        "post_trade_pause_fastreject_sec": post_trade_pause_fastreject_sec,
        "post_trade_pause_loss_sliding_enabled": post_trade_pause_loss_sliding_enabled,
        "post_trade_pause_loss_tier1_pct": post_trade_pause_loss_tier1_pct,
        "post_trade_pause_loss_tier1_sec": post_trade_pause_loss_tier1_sec,
        "post_trade_pause_loss_tier2_pct": post_trade_pause_loss_tier2_pct,
        "post_trade_pause_loss_tier2_sec": post_trade_pause_loss_tier2_sec,
        "post_trade_pause_loss_tier3_pct": post_trade_pause_loss_tier3_pct,
        "post_trade_pause_loss_tier3_sec": post_trade_pause_loss_tier3_sec,
        "post_trade_pause_loss_tier4_pct": post_trade_pause_loss_tier4_pct,
        "post_trade_pause_loss_tier4_sec": post_trade_pause_loss_tier4_sec,
        "post_trade_pause_loss_tier5_sec": post_trade_pause_loss_tier5_sec,
        "direction_exhaustion_enabled": direction_exhaustion_enabled,
        "direction_exhaustion_window_sec": direction_exhaustion_window_sec,
        "direction_exhaustion_profit_count": direction_exhaustion_profit_count,
        "direction_exhaustion_block_sec": direction_exhaustion_block_sec,
        "coin_reentry_penalty_enabled": coin_reentry_penalty_enabled,
        "coin_reentry_penalty_window_sec": coin_reentry_penalty_window_sec,
        "coin_reentry_penalty_per_count": coin_reentry_penalty_per_count,
        "trailing_tp_enabled": trailing_tp_enabled,
        "trailing_tp_min_progress": trailing_tp_min_progress,
        "trailing_tp_follow_low": trailing_tp_follow_low,
        "trailing_tp_follow_mid": trailing_tp_follow_mid,
        "trailing_tp_follow_high": trailing_tp_follow_high,
        "portfolio_sl_rate_enabled": portfolio_sl_rate_enabled,
        "portfolio_sl_rate_window_min": portfolio_sl_rate_window_min,
        "portfolio_sl_rate_threshold": portfolio_sl_rate_threshold,
        "portfolio_sl_rate_pause_min": portfolio_sl_rate_pause_min,
        "btc_b12_combined_cap_enabled": btc_b12_combined_cap_enabled,
        "btc_b12_combined_cap_max": btc_b12_combined_cap_max,
        "override_min_adx": override_min_adx,
        "override_min_mtf_align": override_min_mtf_align,
        "override_min_b12_n": override_min_b12_n,
        "override_require_btc_trend_match": override_require_btc_trend_match,
        "override_max_extra_slots": override_max_extra_slots,
        "override_breakeven_trigger_pct": override_breakeven_trigger_pct,
        "pair_block_enabled": pair_block_enabled,
        "pair_block_mode": pair_block_mode,
        "pair_block_same_limit": pair_block_same_limit,
        "coin_profit_lockin_enabled": coin_profit_lockin_enabled,
        "coin_profit_lockin_window_hours": coin_profit_lockin_window_hours,
        "coin_profit_lockin_min_realized": coin_profit_lockin_min_realized,
        "coin_profit_lockin_protect_ratio": coin_profit_lockin_protect_ratio,
        "coin_profit_lockin_require_be": coin_profit_lockin_require_be,
        "pa_weight_enabled": pa_weight_enabled,
        "pa_weight_pin_bar": pa_weight_pin_bar,
        "pa_weight_engulfing": pa_weight_engulfing,
        "pa_weight_star_v1": pa_weight_star_v1,
        "pa_weight_star_v2": pa_weight_star_v2,
        "pa_weight_squeeze_break": pa_weight_squeeze_break,
        "pa_weight_bos": pa_weight_bos,
        "pa_weight_zone_bonus": pa_weight_zone_bonus,
        "pa_zone_proximity_atr": pa_zone_proximity_atr,
        "pa_location_penalty_far": pa_location_penalty_far,
        "sess_quiet_start_kst": sess_quiet_start_kst,
        "sess_quiet_end_kst": sess_quiet_end_kst,
        "sess_active_start_kst": sess_active_start_kst,
        "sess_active_end_kst": sess_active_end_kst,
        "dm_window_count": dm_window_count,
        "dm_lookback_days": dm_lookback_days,
        "dm_loss_count_penalty": dm_loss_count_penalty,
        "dm_cache_ttl_sec": dm_cache_ttl_sec,
        "btc_regime_ema_short": btc_regime_ema_short,
        "btc_regime_ema_long": btc_regime_ema_long,
        "btc_regime_trans_band_pct": btc_regime_trans_band_pct,
        "btc_regime_slope_flat_thr_pct": btc_regime_slope_flat_thr_pct,
        "btc_regime_bull_long_delta": btc_regime_bull_long_delta,
        "btc_regime_bull_short_delta": btc_regime_bull_short_delta,
        "btc_regime_bear_short_delta": btc_regime_bear_short_delta,
        "btc_regime_trans_delta": btc_regime_trans_delta,
        "btc_regime_cache_ttl_sec": btc_regime_cache_ttl_sec,
        "mb_lookback_trades": mb_lookback_trades,
        "mb_lookback_hours": mb_lookback_hours,
        "mb_dominance_threshold": mb_dominance_threshold,
        "mb_min_total": mb_min_total,
        "mb_cache_ttl_sec": mb_cache_ttl_sec,
        "scanner_min_turnover_24h": scanner_min_turnover_24h,
        "scanner_min_price_usdt": scanner_min_price_usdt,
        "scanner_top_n": scanner_top_n,
        "reverse_drift_atr_tf": reverse_drift_atr_tf,
        "reverse_drift_atr_period": reverse_drift_atr_period,
        "profit_exit_block_min_pnl": profit_exit_block_min_pnl,
        # Context Engine (2026-04-19)
        "session_profile_enabled": session_profile_enabled,
        "direction_memory_enabled": direction_memory_enabled,
        "dm_streak_block_enabled": dm_streak_block_enabled,
        "dm_streak_block": dm_streak_block,
        "dm_streak_block_hours": dm_streak_block_hours,
        "dm_streak_block_opposite": dm_streak_block_opposite,
        # ★ Phase F (2026-04-20): Profit Exit Block 3-tuple
        "profit_exit_block_enabled": profit_exit_block_enabled,
        "profit_exit_block_min_consecutive": profit_exit_block_min_consecutive,
        "profit_exit_block_hours": profit_exit_block_hours,
        "profit_exit_block_block_opposite": profit_exit_block_block_opposite,
        # ★ [2026-05-18] Consecutive Loss Pause (added, was missing)
        "consecutive_loss_pause_enabled": consecutive_loss_pause_enabled,
        "consecutive_loss_pause_count": consecutive_loss_pause_count,
        "consecutive_loss_pause_min": consecutive_loss_pause_min,
        "regime_direction_fail_enabled": regime_direction_fail_enabled,
        "regime_direction_fail_window_hours": regime_direction_fail_window_hours,
        "regime_direction_fail_max": regime_direction_fail_max,
        "btc_regime_enabled": btc_regime_enabled,
        "btc_regime_bear_long_delta": btc_regime_bear_long_delta,
        # When set, applies the same value to bull_short_delta (unifies the counter-direction penalty)
        "btc_regime_bull_short_delta": btc_regime_bear_long_delta,
        "market_bias_enabled": market_bias_enabled,
        "mb_against_delta": mb_against_delta,
        "sess_quiet_delta": sess_quiet_delta,
        "sess_active_delta": sess_active_delta,
        "dm_loss_count_delta": dm_loss_count_delta,
        # B11 Regime Direction Lock (2026-04-19 hard block, 3-axis toggle)
        "regime_direction_lock_enabled": regime_direction_lock_enabled,
        "regime_lock_use_slope": regime_lock_use_slope,
        "regime_lock_use_distance": regime_lock_use_distance,
        "regime_lock_use_cross": regime_lock_use_cross,
        "regime_direction_lock_freeze_sec": regime_direction_lock_freeze_sec,
        "regime_direction_lock_neutral_block": regime_direction_lock_neutral_block,
        # ★ B12 Scanner Breadth Lock (2026-04-23 owner instruction)
        "regime_lock_mode": regime_lock_mode,
        "b12_threshold_n": b12_threshold_n,
        "b12_window_sec": b12_window_sec,
        # Per-direction slot cap (2026-04-20 this agent's instruction)
        "max_long_positions": max_long_positions,
        "max_short_positions": max_short_positions,
        "auto_first_dir_lock": auto_first_dir_lock,
        "regime_reversal_pause_enabled": regime_reversal_pause_enabled,
        "regime_reversal_ema_gap_threshold_pct": regime_reversal_ema_gap_threshold_pct,
        "regime_reversal_adx_threshold": regime_reversal_adx_threshold,
        "regime_reversal_pause_min": regime_reversal_pause_min,
        "conv_sizing_enabled": conv_sizing_enabled,
        "conv_sizing_low_threshold": conv_sizing_low_threshold,
        "conv_sizing_high_threshold": conv_sizing_high_threshold,
        "conv_risk_scale_enabled": conv_risk_scale_enabled,
        "conv_risk_peak_conv": conv_risk_peak_conv,
        "conv_risk_peak_mult": conv_risk_peak_mult,
        "conv_risk_chop_conv": conv_risk_chop_conv,
        "conv_risk_chop_mult": conv_risk_chop_mult,
        "conv_risk_floor_mult": conv_risk_floor_mult,
        "conv_risk_max_mult": conv_risk_max_mult,
        "btc_trend_conv_bonus_enabled": btc_trend_conv_bonus_enabled,
        "btc_trend_conv_bonus": btc_trend_conv_bonus,
        "winners_add_self_growth_enabled": winners_add_self_growth_enabled,
        "winners_add_self_growth_pct": winners_add_self_growth_pct,
        "winners_add_self_growth_ratio_pct": winners_add_self_growth_ratio_pct,
        "multi_be_lock_enabled": multi_be_lock_enabled,
        "multi_be_lock_stage1_pct": multi_be_lock_stage1_pct,
        "multi_be_lock_stage2_pct": multi_be_lock_stage2_pct,
        "multi_be_lock_stage3_pct": multi_be_lock_stage3_pct,
        "multi_be_lock_stage4_pct": multi_be_lock_stage4_pct,
        "multi_be_lock_fee_cushion_pct": multi_be_lock_fee_cushion_pct,
        "be_lock_smart_rsi_check": be_lock_smart_rsi_check,
        "be_lock_smart_candle_check": be_lock_smart_candle_check,
        "be_lock_smart_rsi_long_min": be_lock_smart_rsi_long_min,
        "be_lock_smart_rsi_short_max": be_lock_smart_rsi_short_max,
        "be_lock_smart_candle_count": be_lock_smart_candle_count,
        "parent_roe_guard_enabled": parent_roe_guard_enabled,
        "parent_max_roe_loss_pct": parent_max_roe_loss_pct,
        # ★ Phase J v2 (2026-04-21): skip on ADX fall
        "adx_slope_check_enabled": adx_slope_check_enabled,
        "adx_slope_lookback_bars": adx_slope_lookback_bars,
        "adx_slope_decline_threshold_pct": adx_slope_decline_threshold_pct,
        # ★ Phase K (2026-04-21): Regime Transition Preemptive Entry
        "regime_transition_enabled": regime_transition_enabled,
        "regime_transition_paper_mode": regime_transition_paper_mode,
        "regime_transition_size_mult": regime_transition_size_mult,
        "regime_transition_tp_mult": regime_transition_tp_mult,
        "regime_transition_sl_mult": regime_transition_sl_mult,
        "regime_transition_adx_decline_ratio": regime_transition_adx_decline_ratio,
        "regime_transition_ema_gap_threshold_pct": regime_transition_ema_gap_threshold_pct,
        "regime_transition_min_conviction": regime_transition_min_conviction,
        "regime_transition_last_change_age_min": regime_transition_last_change_age_min,
        "regime_transition_daily_fail_limit": regime_transition_daily_fail_limit,
        "regime_transition_weekly_fail_limit": regime_transition_weekly_fail_limit,
        "regime_transition_min_mtf_align": regime_transition_min_mtf_align,
        # ★ Phase L (2026-04-22): S3 Fee-Aware Gate
        "s3_gate_enabled": s3_gate_enabled,
        "s3_gate_paper_mode": s3_gate_paper_mode,
        "s3_gate_min_net_ev_usdt": s3_gate_min_net_ev_usdt,
        "s3_gate_fee_multiplier": s3_gate_fee_multiplier,
        "s3_gate_slippage_bps": s3_gate_slippage_bps,
        "s3_gate_link_multiplier": s3_gate_link_multiplier,
        # 🪙 Order-book depth size adaptation (2026-06-09)
        "orderbook_depth_sizing_enabled": orderbook_depth_sizing_enabled,
        "orderbook_depth_max_slippage_pct": orderbook_depth_max_slippage_pct,
        "orderbook_depth_min_fill_ratio": orderbook_depth_min_fill_ratio,
        # Phase L.1/L.2/L.3
        "fast_reject_v2_enabled": fast_reject_v2_enabled,
        "fast_reject_v2_max_sec": fast_reject_v2_max_sec,
        "fast_reject_v2_peak_threshold_pct": fast_reject_v2_peak_threshold_pct,
        "fast_reject_v2_pnl_pct": fast_reject_v2_pnl_pct,
        "reentry_cooldown_v2_enabled": reentry_cooldown_v2_enabled,
        "reentry_cooldown_v2_min": reentry_cooldown_v2_min,
        "pa_double_confirm_enabled": pa_double_confirm_enabled,
        "pa_double_confirm_window_sec": pa_double_confirm_window_sec,
        # ★★★ [2026-05-27 owner spirit] Phase 6 — H4/H1 PA + score integration + 5M emergency exit ★★★
        # α. Phase 6 score integration (Master)
        "guard_score_mode_enabled": guard_score_mode_enabled,
        "guard_score_mode_auto_paper": guard_score_mode_auto_paper,
        "guard_score_threshold": guard_score_threshold,
        "regime_align_cap_enabled": regime_align_cap_enabled,
        "regime_align_cap": regime_align_cap,
        "combo_f_dedupe_enabled": combo_f_dedupe_enabled,
        "guard_dir_dedupe_enabled": guard_dir_dedupe_enabled,
        # β. PA Completion (Sig + ไส้หลัง) ⭐
        "pa_completion_enabled": pa_completion_enabled,
        "pa_completion_auto_paper": pa_completion_auto_paper,
        "pa_completion_huikkang_min_ratio": pa_completion_huikkang_min_ratio,
        "pa_completion_lookback_bars": pa_completion_lookback_bars,
        "pa_completion_sig_max_ratio": pa_completion_sig_max_ratio,
        # γ. H4 Pulse Only
        "h4_pulse_only_enabled": h4_pulse_only_enabled,
        "h4_pulse_auto_paper": h4_pulse_auto_paper,
        "h4_pulse_window_min": h4_pulse_window_min,
        "preclose_entry_enabled": preclose_entry_enabled,
        "preclose_min_elapsed_pct": preclose_min_elapsed_pct,
        "preclose_size_ratio": preclose_size_ratio,
        "preclose_wick_ratio_min": preclose_wick_ratio_min,
        "preclose_body_dir_required": preclose_body_dir_required,
        "preclose_max_per_day": preclose_max_per_day,
        "preclose_min_conviction": preclose_min_conviction,
        "preclose_topup_enabled": preclose_topup_enabled,
        "preclose_topup_min_pnl_pct": preclose_topup_min_pnl_pct,
        "preclose_topup_max_chase_pct": preclose_topup_max_chase_pct,
        "preclose_topup_require_candle_dir": preclose_topup_require_candle_dir,
        "preclose_topup_grace_min": preclose_topup_grace_min,
        # δ. H1 PA Pulse
        "h1_pa_pulse_enabled": h1_pa_pulse_enabled,
        "h1_pa_pulse_auto_paper": h1_pa_pulse_auto_paper,
        "h1_pa_pulse_window_min": h1_pa_pulse_window_min,
        "h1_pa_pulse_lookback_bars": h1_pa_pulse_lookback_bars,
        "h1_pa_pulse_min_confidence": h1_pa_pulse_min_confidence,
        "h1_pa_pulse_require_day_dir": h1_pa_pulse_require_day_dir,
        # ε. Anchor Fast-Track
        "anchor_fasttrack_enabled": anchor_fasttrack_enabled,
        "anchor_fasttrack_auto_paper": anchor_fasttrack_auto_paper,
        "anchor_fasttrack_max_proximity": anchor_fasttrack_max_proximity,
        # ζ. Day Box Guard
        "day_box_guard_enabled": day_box_guard_enabled,
        "day_box_guard_auto_paper": day_box_guard_auto_paper,
        "day_box_window_hours": day_box_window_hours,
        "day_box_lock_min_hours": day_box_lock_min_hours,
        "day_box_max_atr_ratio": day_box_max_atr_ratio,
        "day_box_min_touches": day_box_min_touches,
        "day_box_edge_pct": day_box_edge_pct,
        "day_box_breakout_pct": day_box_breakout_pct,
        # η. TF Round TP/SL
        "tf_round_tpsl_enabled": tf_round_tpsl_enabled,
        "tf_round_tpsl_auto_paper": tf_round_tpsl_auto_paper,
        "tf_round_anchor_tf": tf_round_anchor_tf,
        "tf_round_atr_period": tf_round_atr_period,
        "tf_round_tp_atr_mult": tf_round_tp_atr_mult,
        "tf_round_tp2_atr_mult": tf_round_tp2_atr_mult,
        "tf_round_sl_ratio": tf_round_sl_ratio,
        "tf_round_anchor_offset": tf_round_anchor_offset,
        "tf_round_hold_enabled": tf_round_hold_enabled,
        # θ. Frame Guard Option B
        "frame_guard_option_b_enabled": frame_guard_option_b_enabled,
        "frame_guard_option_b_auto_paper": frame_guard_option_b_auto_paper,
        # ι. 5M Emergency Exit ⭐
        "exit_5m_emergency_enabled": exit_5m_emergency_enabled,
        "exit_5m_emergency_auto_paper": exit_5m_emergency_auto_paper,
        "exit_5m_rsi_overbought": exit_5m_rsi_overbought,
        "exit_5m_rsi_oversold": exit_5m_rsi_oversold,
        "exit_5m_bb_top_pct": exit_5m_bb_top_pct,
        "exit_5m_bb_bottom_pct": exit_5m_bb_bottom_pct,
        "exit_5m_min_score": exit_5m_min_score,
        # κ. Guard Score Weights
        "guard_score_pa_completion_ok": guard_score_pa_completion_ok,
        "guard_score_pa_completion_none": guard_score_pa_completion_none,
        "guard_score_d1_pa_ok": guard_score_d1_pa_ok,
        "guard_score_d1_pa_none": guard_score_d1_pa_none,
        "guard_score_btc_aligned": guard_score_btc_aligned,
        "guard_score_btc_opposite": guard_score_btc_opposite,
        "guard_score_adx_strong": guard_score_adx_strong,
        "guard_score_adx_weak": guard_score_adx_weak,
        "guard_score_adx_strong_requires_trend": guard_score_adx_strong_requires_trend,
        "naked_sl_guard_enabled": naked_sl_guard_enabled,
        "naked_sl_guard_buffer_pct": naked_sl_guard_buffer_pct,
        "server_sl_verify_enabled": server_sl_verify_enabled,
        "guard_score_vol_big_align": guard_score_vol_big_align,
        "guard_score_trend_high_conf": guard_score_trend_high_conf,
        "guard_score_trend_low_conf": guard_score_trend_low_conf,
        "guard_score_rsi_extreme": guard_score_rsi_extreme,
        "guard_score_h4_pulse_in": guard_score_h4_pulse_in,
        "guard_score_h4_pulse_out": guard_score_h4_pulse_out,
        "guard_score_h1_pa_in": guard_score_h1_pa_in,
        "guard_score_h1_pa_out": guard_score_h1_pa_out,
        "guard_score_frame_aligned": guard_score_frame_aligned,
        "guard_score_frame_neutral": guard_score_frame_neutral,
        "guard_score_frame_opposite": guard_score_frame_opposite,
        "guard_score_anchor_close": guard_score_anchor_close,
        "guard_score_anchor_far": guard_score_anchor_far,
        "guard_score_day_box_edge": guard_score_day_box_edge,
        "guard_score_day_box_inside": guard_score_day_box_inside,
        "guard_score_microtiming_ok": guard_score_microtiming_ok,
        "guard_score_microtiming_no": guard_score_microtiming_no,
        "guard_score_raw_body_align": guard_score_raw_body_align,
        "guard_score_raw_body_against": guard_score_raw_body_against,
        "guard_score_momentum_deriv_align": guard_score_momentum_deriv_align,
        "guard_score_momentum_deriv_against": guard_score_momentum_deriv_against,
        # ── ★ [2026-06-03 owner] D1 trend weighting + gap-check gate ──
        "d1_trend_weight": d1_trend_weight,
        "cr_speed_sign_guard_enabled": cr_speed_sign_guard_enabled,
        "cr_blowoff_extreme_guard_enabled": cr_blowoff_extreme_guard_enabled,
        "cr_blowoff_extreme_ratio": cr_blowoff_extreme_ratio,
        "cr_trend_agree_guard_enabled": cr_trend_agree_guard_enabled,
        "cr_trend_agree_lookback": cr_trend_agree_lookback,
        "gap_check_enabled": gap_check_enabled,
        "gap_check_tf": gap_check_tf,
        "gap_check_lookback_bars": gap_check_lookback_bars,
        "gap_check_min_pct": gap_check_min_pct,
        "gap_check_atr_adaptive_enabled": gap_check_atr_adaptive_enabled,
        "gap_check_atr_mult": gap_check_atr_mult,
        "gap_check_atr_cap_pct": gap_check_atr_cap_pct,
        "gap_proximity_exit_enabled": gap_proximity_exit_enabled,
        "gap_proximity_exit_tf": gap_proximity_exit_tf,
        "gap_proximity_exit_pct": gap_proximity_exit_pct,
        # ── ★ [2026-06-15 solution B·C] Observation toggles ──
        "gate_ledger_enabled": gate_ledger_enabled,
        "dual_observe_auto_off_weak": dual_observe_auto_off_weak,
    }.items():
        if v is not None:
            patch[k] = v

    # ★ [2026-05-18] Normalize + validate leading_entry_mode
    if leading_entry_mode is not None:
        _le_norm = str(leading_entry_mode).strip().upper()
        if _le_norm not in ("OFF", "CFID", "PATTERN"):
            return {"ok": False, "error": f"leading_entry_mode must be OFF/CFID/PATTERN (got '{leading_entry_mode}')"}
        patch["leading_entry_mode"] = _le_norm

    # scanner_blacklist: convert a comma-separated string → list
    if scanner_blacklist is not None:
        if scanner_blacklist.strip() == "":
            patch["scanner_blacklist"] = []
        else:
            patch["scanner_blacklist"] = [s.strip().upper() for s in scanner_blacklist.split(",") if s.strip()]

    # ★ [2026-05-31 owner server-b race fix] update lock_market only when explicitly provided (None=partial call → keep old value).
    #   On the owner's Save Config the JS always sends an empty string → it gets stored here → next polling returns empty.
    if lock_market is not None:
        patch["lock_market"] = lock_market.strip().upper()

    result = fm.update_config(patch)
    # ★ [2026-05-19] Auto snapshot save (silent fail — does not affect config changes)
    # ★ [2026-06-20] Background thread — the snapshot calls _calc_pnl_24h (full journal parse, ~33s on slow servers),
    #    which was the root cause of the config-save POST blocking that long (drawdown reset 2s vs config 33s). The snapshot is non-essential, so it no longer blocks the response.
    import threading as _th_snap
    _th_snap.Thread(target=_save_config_snapshot, args=(result, patch), daemon=True).start()
    return {"ok": True, "config": result}


# ── Scan / Force-Select ─────────────────────────────────────

@router.get("/scan")
def focus_scan(request: Request):
    """Preview triple-confirmation scan without committing."""
    fm = _get_fm(request)
    try:
        from app.manager.focus_coin_selector import select_focus_coin
        result = select_focus_coin(
            fm.system,
            fm._get_client(),
            direction_mode=fm.config.direction_mode,
            primary_tf=fm.config.primary_tf,
        )
        return {"ok": True, "result": result or {"message": "No candidate passed 3-point confirmation"}}
    except Exception as exc:
        logger.warning("[BINANCE_FUT_API] Scan failed: %s", exc)
        return {"ok": False, "error": str(exc)}


@router.post("/force-select")
def focus_force_select(
    request: Request,
    market: str = Query(..., description="Market symbol (e.g. BTCUSDT)"),
    direction: str = Query("LONG", description="LONG or SHORT"),
):
    """Manually override coin selection (skip SELECTING state)."""
    fm = _get_fm(request)
    result = fm.force_select(market, direction)
    return {"ok": True, **result}


@router.post("/manual-entry")
def focus_manual_entry(
    request: Request,
    market: str = Query(..., description="Market symbol (e.g. BTCUSDT)"),
    direction: str = Query(..., description="LONG or SHORT"),
    wait_for_signal: bool = Query(False, description="[2026-05-29 operator] No immediate execution when True. Auto-executes after the operator confirms the direction signal (default timeout 1 hour)"),
    timeout_sec: Optional[float] = Query(None, description="Smart Manual Entry wait time (default 3600s)"),
):
    """[2026-05-16 owner] Manual forced entry — bypasses gates (microtiming/EE/MTF FLIP).

    Safety guards are kept: reentry / Bybit duplicate / cross-strategy / qty/margin.
    Used when the owner overrides the system's judgment and enters directly.

    [2026-05-29 owner] When wait_for_signal=True, waits in signal-confirmation mode.
    """
    fm = _get_fm(request)
    direction = (direction or "").upper()
    if direction not in ("LONG", "SHORT"):
        return {"ok": False, "error": f"invalid_direction: {direction}"}
    market = (market or "").upper()
    if not market:
        return {"ok": False, "error": "missing_market"}
    try:
        result = fm.manual_entry(market, direction, wait_for_signal=wait_for_signal, timeout_sec=timeout_sec)
        return {"ok": True, "market": market, "direction": direction, **(result or {})}
    except Exception as exc:
        import traceback
        return {"ok": False, "error": str(exc), "trace": traceback.format_exc()[:500]}


@router.get("/pending-manual-entries")
def focus_pending_manual_entries(request: Request):
    """[2026-05-29 owner] Query the Smart Manual Entry waiting queue."""
    fm = _get_fm(request)
    import time as _time
    _now = _time.time()
    _queue = []
    for _q in getattr(fm, "_pending_manual_entries", []) or []:
        _req_ts = float(_q.get("requested_ts", _now))
        _to_sec = float(_q.get("timeout_sec", 3600.0))
        _elapsed = _now - _req_ts
        _queue.append({
            "market": _q.get("market", ""),
            "direction": _q.get("direction", ""),
            "requested_ts": _req_ts,
            "timeout_sec": _to_sec,
            "elapsed_sec": _elapsed,
            "remaining_sec": max(0, _to_sec - _elapsed),
        })
    return {"ok": True, "queue": _queue, "count": len(_queue)}


@router.delete("/pending-manual-entry")
def focus_cancel_pending_manual_entry(
    request: Request,
    market: str = Query(..., description="Market symbol"),
    direction: str = Query(..., description="LONG or SHORT"),
):
    """[2026-05-29 owner] Cancel a Smart Manual Entry queue item."""
    fm = _get_fm(request)
    market = (market or "").upper()
    direction = (direction or "").upper()
    _q = getattr(fm, "_pending_manual_entries", None)
    if _q is None:
        return {"ok": False, "error": "queue_not_initialized"}
    _before = len(_q)
    fm._pending_manual_entries = [
        _e for _e in _q if not (_e.get("market") == market and _e.get("direction") == direction)
    ]
    _after = len(fm._pending_manual_entries)
    return {"ok": True, "market": market, "direction": direction,
            "removed": _before - _after, "remaining": _after}


@router.post("/recover-positions")
def focus_recover_positions(request: Request):
    """Force sync: recover positions from Bybit that FOCUS lost track of."""
    fm = _get_fm(request)
    before = len(fm.positions)
    fm._live_sync_positions()
    after = len(fm.positions)
    restored = after - before
    return {
        "ok": True,
        "before": before,
        "after": after,
        "restored": restored,
        "state": fm.state.value,
        "positions": [{"market": p.market, "direction": p.direction, "qty": p.qty,
                       "entry": p.entry_price} for p in fm.positions],
    }


@router.post("/skip-cooldown")
def focus_skip_cooldown(request: Request):
    """Immediately exit COOLDOWN → DORMANT so FOCUS can scan again."""
    from app.manager.focus_manager import FocusState
    fm = _get_fm(request)
    if fm.state != FocusState.COOLDOWN:
        return {"ok": False, "error": f"Not in COOLDOWN (current: {fm.state.value})"}
    fm.state = FocusState.DORMANT
    fm.cooldown_start_ts = 0
    fm._pending_flip = ""
    fm._save_config()
    logger.info("[FOCUS] COOLDOWN skipped manually → DORMANT")
    return {"ok": True, "state": "DORMANT", "message": "Cooldown skipped"}


@router.post("/lock-market")
def focus_lock_market(
    request: Request,
    market: str = Query("", description="Market to lock (e.g. PAXGUSDT). Empty=unlock"),
):
    """Lock FOCUS to a single market. Empty string to unlock (resume auto-scan)."""
    from app.manager.focus_manager import FocusState
    fm = _get_fm(request)
    fm.config.lock_market = market.upper().strip()
    # When lock is set, switch to that coin immediately
    if fm.config.lock_market:
        fm.selected_market = fm.config.lock_market
        if fm.state.value in ("DORMANT", "ALERT", "COOLDOWN"):
            fm.state = FocusState.HUNT  # run the HUNT logic on the next tick
    else:
        logger.info("[FOCUS] Market unlocked — will auto-scan")
    fm._save_config()
    return {
        "ok": True,
        "lock_market": fm.config.lock_market,
        "message": f"Locked to {fm.config.lock_market}" if fm.config.lock_market else "Unlocked — auto-scan resumed",
    }


# ── Top 10 Live Scanner ─────────────────────────────────────

# [2026-06-12] Short cache of scan results — so re-opens / multiple tabs don't re-run a full 11-coin scan
# (greenpen+conviction+guard, dozens of APIs per coin) every time. TTL 15 seconds.
# ★ engine_warnings (E-STOP/stale) are recomputed fresh on every call even on a cache hit → warnings are never stale.
_SCAN_RESULT_TTL = 15.0
_SCAN_RESULT_CACHE: dict = {}  # top_n -> (ts, results_list)


def _scan_engine_warnings(request, fm) -> list:
    """Engine meta warnings (E-STOP / Scanner stale / FOCUS off) — always fresh, no API calls."""
    import time as _time
    warnings = []
    try:
        _sys = request.app.state.system
        if getattr(_sys, 'emergency_stop', False):
            warnings.append({'level': 'critical', 'tag': 'E_STOP', 'msg': '🆘 Emergency Stop ACTIVE — all entries blocked (Resume required)'})
    except Exception:
        pass
    try:
        _last = float(getattr(fm, 'last_scan_ts', 0) or 0)
        if _last > 0:
            _stale = _time.time() - _last
            if _stale > 180:  # 3min+ stale
                warnings.append({'level': 'warn', 'tag': 'SCAN_STALE', 'msg': f'⚠️ Scanner stale {int(_stale)}s — engine may be stalled'})
    except Exception:
        pass
    try:
        if not bool(getattr(fm.config, 'enabled', True)):
            warnings.append({'level': 'warn', 'tag': 'FOCUS_OFF', 'msg': 'ℹ️ FOCUS Strategy DISABLED'})
    except Exception:
        pass
    return warnings


@router.get("/scan-list")
def focus_scan_list(
    request: Request,
    top_n: int = Query(10, ge=3, le=20),
):
    """Scan top coins with GreenPen analysis — returns ranked list."""
    fm = _get_fm(request)
    try:
        from app.strategy.greenpen import full_analysis
        from app.strategy.greenpen.pa_detector import OHLCV
        import time as _scan_t

        # ── #2 Scan-result cache hit (instant for re-opens / multiple tabs) ──
        _ck = int(top_n)
        _hit = _SCAN_RESULT_CACHE.get(_ck)
        if _hit and (_scan_t.time() - _hit[0]) < _SCAN_RESULT_TTL:
            _items = _hit[1]
            return {"ok": True, "items": _items, "count": len(_items),
                    "engine_warnings": _scan_engine_warnings(request, fm),
                    "cached": True, "cache_age": round(_scan_t.time() - _hit[0], 1)}

        # 1) Get top coins by 24h turnover (linear) — client seam (= Binance)
        tickers = fm._get_client().get_market_tickers()

        # ★ [2026-06-13 owner] The greenpen scanner is consistent with the entry filter (scanner_min_price_usdt) — excludes low-price coins
        _scan_min_price = float(getattr(fm.config, "scanner_min_price_usdt", 0.0) or 0.0)
        scored = []
        for t in tickers:
            if not isinstance(t, dict):
                continue
            symbol = str(t.get("symbol", ""))
            if not symbol.endswith("USDT"):
                continue
            turnover = float(t.get("turnover24h", 0) or 0)
            price = float(t.get("lastPrice", 0) or 0)
            change = float(t.get("price24hPcnt", 0) or 0) * 100
            if turnover < 1_000_000:
                continue
            if _scan_min_price > 0 and 0 < price < _scan_min_price:  # ★ exclude low-price coins from the scanner too
                continue
            scored.append({"symbol": symbol, "turnover": turnover, "price": price, "change_pct": change})

        scored.sort(key=lambda x: -x["turnover"])
        candidates = scored[:top_n]

        # 2) Run GreenPen analysis on each
        client = fm._get_client()
        results = []
        for c in candidates:
            symbol = c["symbol"]
            try:
                raw = client.get_kline(symbol, interval="60", limit=60)
                candles = []
                for r in raw:
                    try:
                        candles.append(OHLCV(
                            open=float(r[1]), high=float(r[2]),
                            low=float(r[3]), close=float(r[4]),
                            volume=float(r[5]) if len(r) > 5 else 0,
                        ))
                    except (IndexError, TypeError, ValueError):
                        continue

                if len(candles) < 10:
                    results.append({
                        "market": symbol, "signal": "-", "pa_pattern": "-",
                        "trend": "-", "confidence": 0, "atr": 0, "adx": 0, "zones": 0,
                        "conviction": 0, "status": "-",
                        "price": c["price"], "change_pct": c["change_pct"],
                    })
                    continue

                gp = full_analysis(candles)

                # Determine signal
                signal = "HOLD"
                pa_name = "-"
                conf = 0
                pa_type = "none"  # "pa" = candlestick pattern, "structure" = market structure, "none"
                if gp.pa_signals:
                    best = gp.pa_signals[0]
                    signal = "BUY" if best.direction.value == "LONG" else "SELL"
                    pa_name = best.pattern.value
                    conf = round(best.confidence * 100)
                    pa_type = "pa"

                # When there's no PA pattern → fall back to Market Structure
                if pa_type == "none":
                    _struct = gp.structure
                    _s_conf = round(float(getattr(_struct, "confidence", 0) or 0) * 100)

                    # Show first when a BOS (Break of Structure) is detected
                    _bos = getattr(_struct, "bos", None)
                    if _bos and getattr(_bos, "detected", False):
                        pa_name = f"BOS_{_bos.direction}"
                        conf = max(_s_conf, 60)
                        pa_type = "bos"
                        signal = "BUY" if _bos.direction == "BULLISH" else "SELL"
                    elif _struct.trend.value != "SIDEWAYS":
                        # In a trend: show the recent swing pattern (HH/HL or LH/LL)
                        _swings = getattr(_struct, "swings", []) or []
                        if len(_swings) >= 2:
                            _last2 = [s.type.value for s in _swings[-2:]]
                            pa_name = "/".join(_last2)
                        else:
                            pa_name = "TREND"
                        conf = _s_conf
                        pa_type = "structure"
                    else:
                        # Ranging: show the SW range
                        _sw = getattr(_struct, "sw_range", None)
                        pa_name = "RANGE"
                        conf = _s_conf
                        pa_type = "structure"

                # ADX calculation
                _adx_val = 0
                try:
                    from app.strategy.indicators import adx as _adx_fn
                    _highs = [c.high for c in candles]
                    _lows = [c.low for c in candles]
                    _closes = [c.close for c in candles]
                    _adx_result = _adx_fn(_highs, _lows, _closes, period=14)
                    if _adx_result:
                        _adx_val = round(_adx_result.get("adx", 0), 1)
                except Exception:
                    pass

                # ── Conviction score (★ 2026-05-11 Phase 1 integration) ──
                # Call focus_manager's _compute_conviction_score directly — matches the entry-decision score
                # Reflects both PA Pattern (0~6) + Phase 1 penalties (MTF Conflict + Momentum Reversal)
                _direction = "LONG" if signal == "BUY" else ("SHORT" if signal == "SELL" else "")
                try:
                    # Pass zones too (for applying the PA Pattern's zone bonus)
                    _zones_tuple = None
                    try:
                        _zones_list = getattr(gp, "zones", []) or []
                        if _zones_list:
                            _first = _zones_list[0]
                            _zones_tuple = (float(_first.price_low), float(_first.price_high))
                    except Exception:
                        _zones_tuple = None
                    _conv = fm._compute_conviction_score(symbol, candles, direction=_direction, zones=_zones_tuple)
                    # ★ [2026-05-17] Copy the breakdown immediately — prevents it being overwritten when the next coin is evaluated
                    _conv_dbg = dict(getattr(fm, '_last_conviction_breakdown', {}) or {})
                    # ★ [2026-05-17] If there's a final scanner-applied conviction, prefer it (reflects BB-position penalty etc.)
                    _scan_final_conv = (getattr(fm, '_last_scan_conviction', {}) or {}).get(symbol)
                    if _scan_final_conv is not None:
                        _conv = _scan_final_conv
                except Exception:
                    # Fallback: simple ADX-based (old logic)
                    _conv = 0
                    _conv_dbg = {}
                    if _adx_val >= 40: _conv = 3
                    elif _adx_val >= 30: _conv = 2
                    elif _adx_val >= 20: _conv = 1

                # ── Scanner status ──
                _status = "READY"
                _held_mkts = {p.market for p in fm.positions} if hasattr(fm, 'positions') else set()
                if symbol in _held_mkts:
                    _status = "HELD"
                elif hasattr(fm, '_last_exit_market') and fm._last_exit_market == symbol:
                    import time as _time
                    _elapsed = _time.time() - getattr(fm, '_last_exit_ts', 0)
                    if _elapsed < 300:
                        _status = f"COOL {int(300-_elapsed)}s"

                # ★ [2026-05-17] Scanner cycle block reason (UI STATUS column)
                _block_reason = ""
                try:
                    _scan_cache = getattr(fm, '_last_scan_filter', None) or {}
                    _block_reason = (_scan_cache.get('items', {}) or {}).get(symbol, "")
                except Exception:
                    pass

                # ── [2026-05-20 Phase 6 Stage 6] Energy-bar change rate + time series (for the UI sparkline) ──
                _conf_delta_pp = 0.0
                _conf_samples = 0
                _conf_history = []
                try:
                    if hasattr(fm, '_get_trend_velocity'):
                        _vel = fm._get_trend_velocity(symbol, lookback_sec=300.0)
                        _conf_delta_pp = round(_vel.get('delta_pp', 0.0), 1)
                        _conf_samples = int(_vel.get('samples', 0))
                    _hist_deque = getattr(fm, '_confidence_history', {}).get(symbol)
                    if _hist_deque:
                        # Only the last N for the sparkline (omit timestamps, confidence% only)
                        _conf_history = [round(float(e[2]) * 100, 1) for e in list(_hist_deque)[-30:]]
                except Exception:
                    pass

                # ★ [2026-05-28] Guard-score cache (the guard_score result from _evaluate_entry)
                _gs = dict((getattr(fm, '_last_guard_score', {}) or {}).get(symbol.upper(), {}) or {})
                # If not cached, evaluate directly for BUY/SELL coins only (owner 5-28: 4 columns on every row)
                if not _gs and signal in ("BUY", "SELL") and hasattr(fm, '_compute_guard_score_modifiers'):
                    try:
                        _dir_tn = "LONG" if signal == "BUY" else "SHORT"
                        _gs_entry_tn = {"conviction_score": _conv, "market": symbol, "direction": _dir_tn}
                        _gs_total_tn, _gs_breakdown_tn = fm._compute_guard_score_modifiers(symbol, _dir_tn, _gs_entry_tn)
                        _final_tn = float(_conv or 0) + float(_gs_total_tn or 0)
                        _disp_tn = 0.0 if abs(_final_tn) < 0.05 else _final_tn
                        _bd_tn = (_gs_breakdown_tn or "").replace(" | ", ",").replace("++", "+")
                        _gs = {
                            "base": float(_conv or 0),
                            "deduction": float(_gs_total_tn or 0),
                            "total": _disp_tn,
                            "threshold": float(getattr(fm.config, "guard_score_threshold", 65.0)),
                            "breakdown": _bd_tn,
                        }
                    except Exception as _gse_tn:
                        logger.debug("[BINANCE_FUT_API] %s guard_score eval failed: %s", symbol, _gse_tn)
                # ★ [2026-05-28 owner] Bot-opinion warning badge (BB conflict / trend conflict / negative score etc.)
                _bot_op = _bot_opinion(signal, gp.structure.trend.value,
                                       _gs.get("total"), _gs.get("threshold"),
                                       _block_reason, pa_name)
                results.append({
                    "market": symbol,
                    "signal": signal,
                    "pa_pattern": pa_name,
                    "pa_type": pa_type,
                    "trend": gp.structure.trend.value,
                    "confidence": conf,
                    "confidence_delta_pp": _conf_delta_pp,        # ★ Phase 6 Stage 6: 5-minute change rate (%p)
                    "confidence_samples": _conf_samples,          # number of time-series data points
                    "confidence_history": _conf_history,          # last 30 confidence % (sparkline)
                    "atr": round(gp.atr, 2),
                    "adx": _adx_val,
                    "zones": len(gp.zones),
                    "conviction": _conv,
                    "conviction_breakdown": _conv_dbg,  # ★ Phase 5 per-item scores (UI tooltip)
                    "block_reason": _block_reason,  # ★ Scanner cycle block reason
                    "status": _status,
                    # ★ [2026-05-28] For the separate guard-score column
                    "guard_base": _gs.get("base"),
                    "guard_deduction": _gs.get("deduction"),
                    "guard_total": _gs.get("total"),
                    "guard_threshold": _gs.get("threshold"),
                    "guard_breakdown": _gs.get("breakdown"),
                    "bot_opinion": _bot_op,  # ★ [2026-05-28] bot opinion (None if absent)
                    "price": c["price"],
                    "change_pct": round(c["change_pct"], 1),
                })
            except Exception as exc:
                results.append({
                    "market": symbol, "signal": "ERR", "pa_pattern": str(exc)[:30],
                    "trend": "-", "confidence": 0, "atr": 0, "adx": 0, "zones": 0,
                    "conviction": 0, "status": "ERR",
                    "price": c["price"], "change_pct": round(c.get("change_pct", 0), 1),
                })

        # ★ If lock_market isn't in the Top N, add a separate analysis (owner 5-28: the gold default first row even when empty)
        _lock = (fm.config.lock_market or "").upper() or "XAUTUSDT"
        if _lock and not any(r["market"] == _lock for r in results):
            try:
                _lk_raw = client.get_kline(_lock, interval="60", limit=60)
                _lk_candles = [OHLCV(open=float(r[1]), high=float(r[2]), low=float(r[3]), close=float(r[4]),
                               volume=float(r[5]) if len(r) > 5 else 0) for r in _lk_raw if len(r) >= 5]
                if len(_lk_candles) >= 10:
                    _lk_gp = full_analysis(_lk_candles)
                    _lk_signal = "HOLD"
                    _lk_pa = "-"
                    _lk_conf = 0
                    _lk_pa_type = "none"
                    if _lk_gp.pa_signals:
                        _lk_best = _lk_gp.pa_signals[0]
                        _lk_signal = "BUY" if _lk_best.direction.value == "LONG" else "SELL"
                        _lk_pa = _lk_best.pattern.value
                        _lk_conf = round(_lk_best.confidence * 100)
                        _lk_pa_type = "pa"
                    if _lk_pa_type == "none":
                        _lk_struct = _lk_gp.structure
                        _lk_sconf = round(float(getattr(_lk_struct, "confidence", 0) or 0) * 100)
                        _lk_bos = getattr(_lk_struct, "bos", None)
                        if _lk_bos and getattr(_lk_bos, "detected", False):
                            _lk_pa = f"BOS_{_lk_bos.direction}"
                            _lk_conf = max(_lk_sconf, 60)
                            _lk_pa_type = "bos"
                            _lk_signal = "BUY" if _lk_bos.direction == "BULLISH" else "SELL"
                        elif _lk_struct.trend.value != "SIDEWAYS":
                            _lk_swings = getattr(_lk_struct, "swings", []) or []
                            _lk_pa = "/".join([s.type.value for s in _lk_swings[-2:]]) if len(_lk_swings) >= 2 else "TREND"
                            _lk_conf = _lk_sconf
                            _lk_pa_type = "structure"
                        else:
                            _lk_pa = "RANGE"
                            _lk_conf = _lk_sconf
                            _lk_pa_type = "structure"
                    _lk_adx = 0
                    try:
                        from app.strategy.indicators import adx as _adx_fn2
                        _lk_h = [c.high for c in _lk_candles]
                        _lk_l = [c.low for c in _lk_candles]
                        _lk_c = [c.close for c in _lk_candles]
                        _lk_r = _adx_fn2(_lk_h, _lk_l, _lk_c, period=14)
                        if _lk_r:
                            _lk_adx = round(_lk_r.get("adx", 0), 1)
                    except Exception:
                        pass
                    # lock_market price info
                    _lk_price = _lk_candles[-1].close if _lk_candles else 0
                    # conviction for lock_market (★ 2026-05-11 Phase 1 integration)
                    _lk_direction = "LONG" if _lk_signal == "BUY" else ("SHORT" if _lk_signal == "SELL" else "")
                    try:
                        _lk_zones_tuple = None
                        _lk_zones_list = getattr(_lk_gp, "zones", []) or []
                        if _lk_zones_list:
                            _lk_first = _lk_zones_list[0]
                            _lk_zones_tuple = (float(_lk_first.price_low), float(_lk_first.price_high))
                        _lk_conv = fm._compute_conviction_score(_lock, _lk_candles, direction=_lk_direction, zones=_lk_zones_tuple)
                        _lk_conv_dbg = dict(getattr(fm, '_last_conviction_breakdown', {}) or {})
                        # ★ Prefer the final scanner conviction
                        _lk_scan_final = (getattr(fm, '_last_scan_conviction', {}) or {}).get(_lock)
                        if _lk_scan_final is not None:
                            _lk_conv = _lk_scan_final
                    except Exception:
                        _lk_conv = 0
                        _lk_conv_dbg = {}
                        if _lk_adx >= 40: _lk_conv = 3
                        elif _lk_adx >= 30: _lk_conv = 2
                        elif _lk_adx >= 20: _lk_conv = 1
                    _lk_status = "HELD" if _lock in {p.market for p in fm.positions} else "READY"
                    _lk_block = ""
                    try:
                        _lk_block = ((getattr(fm, '_last_scan_filter', None) or {}).get('items', {}) or {}).get(_lock, "")
                    except Exception:
                        pass
                    # ★ [2026-05-28 owner] Evaluate guard_score for lock_market too — fills the 4 columns
                    _lk_gs_data = {}
                    try:
                        # Prefer the cache (when it already passed _evaluate_entry)
                        _lk_gs_data = dict((getattr(fm, '_last_guard_score', {}) or {}).get(_lock, {}) or {})
                        # Evaluate directly if not cached or the direction matches
                        if not _lk_gs_data and _lk_direction and hasattr(fm, '_compute_guard_score_modifiers'):
                            _lk_gs_entry = {"conviction_score": _lk_conv, "market": _lock, "direction": _lk_direction}
                            _lk_gs_total, _lk_gs_breakdown = fm._compute_guard_score_modifiers(_lock, _lk_direction, _lk_gs_entry)
                            _lk_final = float(_lk_conv or 0) + float(_lk_gs_total or 0)
                            _lk_disp_total = 0.0 if abs(_lk_final) < 0.05 else _lk_final
                            _lk_bd_short = (_lk_gs_breakdown or "").replace(" | ", ",").replace("++", "+")
                            _lk_gs_data = {
                                "base": float(_lk_conv or 0),
                                "deduction": float(_lk_gs_total or 0),
                                "total": _lk_disp_total,
                                "threshold": float(getattr(fm.config, "guard_score_threshold", 65.0)),
                                "breakdown": _lk_bd_short,
                            }
                    except Exception as _gse:
                        logger.debug("[BINANCE_FUT_API] lock_market guard_score eval failed: %s", _gse)
                    # ★ [2026-05-28] Bot opinion (applied to lock_market too)
                    _lk_bot_op = _bot_opinion(_lk_signal, _lk_gp.structure.trend.value,
                                              _lk_gs_data.get("total"), _lk_gs_data.get("threshold"),
                                              _lk_block, _lk_pa)
                    results.append({
                        "market": _lock, "signal": _lk_signal, "pa_pattern": _lk_pa,
                        "pa_type": _lk_pa_type, "trend": _lk_gp.structure.trend.value,
                        "confidence": _lk_conf, "atr": round(_lk_gp.atr, 2),
                        "adx": _lk_adx, "zones": len(_lk_gp.zones),
                        "conviction": _lk_conv,
                        "conviction_breakdown": _lk_conv_dbg,  # ★ Phase 5 per-item scores
                        "block_reason": _lk_block,  # ★ Scanner cycle block reason
                        "status": _lk_status,
                        # ★ [2026-05-28] Separate guard_score column
                        "guard_base": _lk_gs_data.get("base"),
                        "guard_deduction": _lk_gs_data.get("deduction"),
                        "guard_total": _lk_gs_data.get("total"),
                        "guard_threshold": _lk_gs_data.get("threshold"),
                        "guard_breakdown": _lk_gs_data.get("breakdown"),
                        "bot_opinion": _lk_bot_op,  # ★ [2026-05-28] bot opinion
                        "price": _lk_price, "change_pct": 0,
                        "_is_lock": True,  # sorting marker
                    })
            except Exception as _lke:
                logger.debug("[BINANCE_FUT_API] lock_market scan failed: %s", _lke)

        # Sort: lock_market always first row (owner 5-28 "dedicated gold space") → BUY/SELL → confidence
        signal_order = {"BUY": 0, "SELL": 1, "HOLD": 2, "ERR": 3, "-": 4}
        results.sort(key=lambda x: (
            0 if (x.get("_is_lock") or x.get("market", "").upper() == _lock) else 1,
            signal_order.get(x["signal"], 9),
            -x["confidence"]
        ))

        # ── #2 Save the scan-result cache (only on success — prevents caching errors) ──
        _SCAN_RESULT_CACHE[_ck] = (_scan_t.time(), results)

        # ★ [2026-05-17] Engine meta state — addresses the owner's 9-month trauma ("engine/E-STOP/Auto Engine")
        # ★ Always fresh (same helper as the cache-hit path) → E-STOP/stale warnings are never stale.
        engine_warnings = _scan_engine_warnings(request, fm)
        return {"ok": True, "items": results, "count": len(results), "engine_warnings": engine_warnings}
    except Exception as exc:
        logger.warning("[BINANCE_FUT_API] scan-list failed: %s", exc)
        return {"ok": False, "error": str(exc)}


# ── Zones / Analysis ────────────────────────────────────────

@router.get("/zones")
def focus_zones(request: Request):
    """Current Zone settings for selected market."""
    fm = _get_fm(request)
    return {
        "ok": True,
        "market": fm.selected_market,
        "zones": fm.zones,
        "primary_sig": fm.primary_sig,  # [2026-05-15 h4_sig→primary_sig]
    }


@router.get("/tf-progress")
def focus_tf_progress(
    request: Request,
    market: str = Query("BTCUSDT", description="Market e.g. BTCUSDT"),
):
    """In-progress bar info for the 7 TFs (D/H4/H1/30M/15M/5M/3M) — for manual-entry reference.

    [2026-05-21] Owner decision. Visualizes the flow before a bar closes. No change to entry logic.
    """
    fm = _get_fm(request)
    if not fm:
        return {"ok": False, "error": "no_fm"}
    try:
        return fm._compute_tf_progress(market.upper())
    except Exception as exc:
        return {"ok": False, "market": market, "error": str(exc)}


@router.get("/analysis/{market}")
def focus_analysis(
    request: Request,
    market: str,
    tf: str = Query("240", description="Timeframe: 1,5,15,60,240,D"),
):
    """Run GreenPen full analysis on any market (preview)."""
    fm = _get_fm(request)
    try:
        from app.strategy.greenpen import full_analysis
        from app.strategy.greenpen.pa_detector import OHLCV

        raw = fm._get_client().get_kline(market.upper(), interval=tf, limit=50)
        candles = []
        for r in raw:
            try:
                candles.append(OHLCV(
                    open=float(r[1]), high=float(r[2]),
                    low=float(r[3]), close=float(r[4]),
                    volume=float(r[5]) if len(r) > 5 else 0,
                ))
            except (IndexError, TypeError, ValueError):
                continue

        gp = full_analysis(candles)

        return {
            "ok": True,
            "market": market.upper(),
            "tf": tf,
            "structure": {
                "trend": gp.structure.trend.value,
                "confidence": gp.structure.confidence,
                "swings": [{"type": s.type.value, "price": s.price, "idx": s.candle_idx}
                           for s in gp.structure.swings[-6:]],
                "bos": {"direction": gp.structure.bos.direction, "price": gp.structure.bos.break_price}
                       if gp.structure.bos else None,
            },
            "zones": [{"type": z.type.value, "low": round(z.price_low, 2), "high": round(z.price_high, 2),
                        "strength": round(z.strength, 2)} for z in gp.zones],
            "pa_signals": [{"pattern": p.pattern.value, "direction": p.direction.value,
                            "confidence": round(p.confidence, 2)} for p in gp.pa_signals],
            "atr": round(gp.atr, 4),
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ============================================================
# Trade Journal — FOCUS + Harpoon ledger
# ============================================================

# Peer Brief Scanner response cache — the near-miss post-hoc enrichment fetches, for each near_miss,
# the current price + 5/15/30/60-min candles, which is heavy. If many tabs/servers poll every 20s they hit the kline
# wall and responses speed up and slow down (=panel flicker). The whole fleet shares the same payload, so a short TTL lets it be reused.
# [2026-06-19 owner "it's not smooth"] = the 4th dashboard-slowness anti-pattern (journal leak → b12 full parse → kline wall).
_PEER_CACHE_RESP_BOX: dict = {"ts": 0.0, "data": None}
_PEER_CACHE_RESP_TTL = 25.0   # seconds — slightly above the dashboard's 20s polling so consecutive polls hit (smooth even for a single tab).
                              #       post-hoc judgments are on a 5~60min scale, so 25s staleness is negligible.


@router.get("/peer-cache")
def focus_peer_cache(request: Request):
    """For the Peer Brief Scanner — combines peer-server caches + this (Home) server's brief (read-only, no extra polling).
    servers[] = [self, peer1, peer2...] uniform format: positions/near_miss/losses/wins (2026-06-07 owner).
    ★ Caches the response for _PEER_CACHE_RESP_TTL seconds → multiple tabs/polls reuse one heavy enrichment (smoothness)."""
    # ★ [2026-06-23 owner confirmed] The fleet peer-brief protocol (/peer/brief, build_my_brief) tracks only *Bybit FOCUS*
    #   positions. There is no Binance-running peer in the fleet, so empty values are normal in the Binance window.
    #   To avoid mixing Bybit fleet data into the Binance screen, return an empty response here (blocks the leak).
    #   (Remove this guard once the fleet protocol becomes exchange-aware.)
    return {"ok": True, "exchange": "binance_futures",
            "note": "The fleet peer-brief currently tracks only Bybit FOCUS · no Binance peer (empty is normal)"}
    import time as _t  # noqa: F841 (original logic below — when switching to exchange-aware, just remove the guard above)
    from app.core import peer_brief as pb
    now = _t.time()
    _box = _PEER_CACHE_RESP_BOX
    if _box.get("data") is not None and (now - float(_box.get("ts") or 0.0)) < _PEER_CACHE_RESP_TTL:
        return _box["data"]
    snap = pb.get_cache_snapshot()
    servers = []
    fm = None

    def _f(v, default=0.0):
        try:
            return float(v)
        except (TypeError, ValueError):
            return default

    def _gate(reason: str) -> str:
        r = str(reason or "").strip()
        return r.split(":", 1)[0].split("(", 1)[0].strip() or "?"

    def _direction_return(direction: str, px0: float, px1: float):
        if px0 <= 0 or px1 <= 0:
            return None
        raw = (px1 / px0 - 1.0) * 100.0
        return raw if str(direction or "").upper() == "LONG" else -raw

    _price_cache = {}
    _kline_cache = {}

    def _current_price(symbol: str) -> float:
        sym = str(symbol or "").upper()
        if not sym or fm is None:
            return 0.0
        if sym in _price_cache:
            return _price_cache[sym]
        try:
            px = _f(fm._get_current_price(sym), 0.0)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[FOCUS] peer-cache current price failed %s: %s", sym, exc)
            px = 0.0
        _price_cache[sym] = px if px > 0 else 0.0
        return _price_cache[sym]

    def _close_at_or_after(symbol: str, ts0: float, target_ts: float) -> float:
        sym = str(symbol or "").upper()
        if not sym or fm is None or ts0 <= 0 or target_ts <= 0 or target_ts > now:
            return 0.0
        age_min = max(0.0, (now - ts0) / 60.0)
        limit = max(24, min(144, int(age_min / 5.0) + 18))
        ck = (sym, limit)
        raw = _kline_cache.get(ck)
        if raw is None:
            try:
                getter = getattr(fm, "_get_mtf_kline", None)
                raw = getter(sym, "5", limit=limit, ttl=30.0) if callable(getter) else fm._get_client().get_kline(sym, interval="5", limit=limit)
            except Exception as exc:  # noqa: BLE001
                logger.debug("[FOCUS] peer-cache kline failed %s: %s", sym, exc)
                raw = []
            _kline_cache[ck] = raw or []
        for row in raw or []:
            try:
                ts = _f(row[0], 0.0)
                if ts > 10_000_000_000:
                    ts = ts / 1000.0
                close = _f(row[4], 0.0)
            except (IndexError, TypeError, ValueError):
                continue
            if close > 0 and (ts + 300.0) >= target_ts:
                return close
        return 0.0

    def _verdict_label(age_min: float, ret_now):
        if ret_now is None:
            return ("unknown", "Awaiting judgment")
        if age_min < 5.0:
            return ("watching", "Watching")
        # ret_now = if we had entered in the blocked direction, how far it has gone now.
        # Positive=missed profit, zero/negative=blocking was favorable or it ranged.
        if ret_now > 0.10:
            return ("missed_entry", "Regrettable block")
        if ret_now <= 0.05:
            return ("good_block", "Good block")
        return ("neutral", "Neutral")

    def _enrich_near_miss(n: dict) -> dict:
        out = dict(n or {})
        sym = str(out.get("symbol") or "").upper()
        direction = str(out.get("direction") or "").upper()
        ts0 = _f(out.get("ts"), 0.0)
        age_min = _f(out.get("age_min"), round((now - ts0) / 60.0, 1) if ts0 else 0.0)
        block_price = _f(out.get("price") or out.get("block_price"), 0.0)
        cur = _current_price(sym)
        ret_now = _direction_return(direction, block_price, cur)
        if ret_now is not None:
            ret_now = round(ret_now, 3)
        vkey, vlabel = _verdict_label(age_min, ret_now)
        out.update({
            "symbol": sym,
            "direction": direction,
            "ts": ts0,
            "age_min": round(age_min, 1),
            "price": block_price,
            "block_price": block_price,
            "current_price": cur,
            "ret_now_pct": ret_now,
            "gate": _gate(out.get("reason")),
            "verdict": vkey,
            "verdict_label": vlabel,
        })
        for h in (5, 15, 30, 60):
            key = f"ret_{h}m_pct"
            px_key = f"price_{h}m"
            if age_min < h or block_price <= 0:
                out[key] = None
                out[px_key] = 0.0
                continue
            px_h = _close_at_or_after(sym, ts0, ts0 + h * 60.0)
            ret_h = _direction_return(direction, block_price, px_h)
            out[key] = round(ret_h, 3) if ret_h is not None else None
            out[px_key] = px_h
        return out

    # self (Home) first
    try:
        fm = _get_fm(request)
        mb = pb.build_my_brief(getattr(fm, "system", None))
        servers.append({
            "server_id": mb.server_id, "self": True, "ok_age_sec": 0, "stale": False,
            "positions": [{"symbol": p.symbol, "direction": p.direction, "age_min": p.age_min,
                           "peak_pnl_pct": p.peak_pnl_pct, "pnl_pct": p.pnl_pct, "pnl_usdt": p.pnl_usdt} for p in mb.active_positions],
            "near_miss": [{"symbol": n.symbol, "direction": n.direction, "score": n.score, "reason": n.reason,
                           "ts": n.ts, "price": n.price,
                           "age_min": round((now - n.ts) / 60.0, 1) if n.ts else 0} for n in mb.recent_near_miss],
            "losses": [{"symbol": x.symbol, "direction": x.direction, "pnl_net": x.pnl_net,
                        "age_min": round((now - x.ts) / 60.0, 1) if x.ts else 0} for x in mb.recent_losses],
            "wins": [{"symbol": x.symbol, "direction": x.direction, "pnl_net": x.pnl_net,
                      "age_min": round((now - x.ts) / 60.0, 1) if x.ts else 0} for x in mb.recent_wins],
        })
    except Exception as exc:
        logger.debug("[FOCUS] peer-cache self build failed: %s", exc)
    for p in snap.get("peers", []):
        q = dict(p); q["self"] = False
        servers.append(q)
    for srv in servers:
        try:
            srv["near_miss"] = [_enrich_near_miss(n) for n in (srv.get("near_miss") or []) if isinstance(n, dict)]
        except Exception as exc:  # noqa: BLE001 — keep the raw near_miss even if enrichment fails (so the panel isn't empty)
            logger.debug("[FOCUS] peer-cache enrich failed for %s: %s", srv.get("server_id"), exc)
    snap["servers"] = servers
    _PEER_CACHE_RESP_BOX["ts"] = now
    _PEER_CACHE_RESP_BOX["data"] = snap
    return snap


@router.get("/journal")
def focus_journal(
    limit: int = Query(50, ge=1, le=500),
    page: int = Query(1, ge=1, le=10000),
    strategy: str = Query("", description="FOCUS or HARPOON (empty=all)"),
    market: str = Query("", description="Market filter e.g. BTCUSDT"),
    include_blocked: bool = Query(False, description="Include BLOCKED events (default=hide)"),
):
    """Query the FOCUS + Harpoon trade ledger (pagination + coin filter)."""
    try:
        from app.manager.trade_journal import journal
        result = _bjournal().get_trades(
            limit=limit, strategy=strategy, market=market,
            include_blocked=include_blocked, page=page,
        )
        total_count = result["total_count"]
        total_pages = max(1, -(-total_count // limit))  # ceil division
        return {
            "ok": True,
            "trades": result["trades"],
            "count": len(result["trades"]),
            "total_count": total_count,
            "page": page,
            "total_pages": total_pages,
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@router.get("/journal/markets")
def focus_journal_markets():
    """Query the list of available markets."""
    try:
        from app.manager.trade_journal import journal
        markets = _bjournal().get_markets()
        return {"ok": True, "markets": markets}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@router.get("/journal/summary")
def focus_journal_summary():
    """FOCUS + Harpoon performance summary (includes a Dynamic Trailing comparison)."""
    try:
        from app.manager.trade_journal import journal
        summary = _bjournal().get_summary()
        return {"ok": True, "summary": summary}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ── Daily Performance Snapshots ────────────────────────────

@router.get("/daily-snapshots")
def focus_daily_snapshots():
    """Query all saved daily performance snapshots (for charts)."""
    try:
        from app.manager.focus_daily_snapshot import get_all_snapshots
        snapshots = get_all_snapshots(_BINANCE_FUT_SNAP_DIR)
        return {"ok": True, "snapshots": snapshots, "count": len(snapshots)}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@router.get("/daily-snapshots/{date}")
def focus_daily_snapshot_detail(date: str):
    """Query the detail of a specific date's snapshot."""
    try:
        from app.manager.focus_daily_snapshot import load_snapshot
        snap = load_snapshot(date, _BINANCE_FUT_SNAP_DIR)
        if snap is None:
            return {"ok": False, "error": f"No snapshot for {date}"}
        return {"ok": True, "snapshot": snap}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@router.post("/daily-snapshots/backfill")
def focus_daily_snapshot_backfill(request: Request):
    """Batch-generate past snapshots from the journal (only missing dates)."""
    try:
        from app.manager.focus_daily_snapshot import backfill_from_journal
        fm = _get_fm(request)
        from dataclasses import asdict
        config = asdict(fm.config)
        count = backfill_from_journal(config, journal_path=_BINANCE_FUT_JOURNAL_PATH, snap_dir=_BINANCE_FUT_SNAP_DIR)
        return {"ok": True, "created": count}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@router.post("/daily-snapshots/save-today")
def focus_daily_snapshot_save_today(request: Request):
    """Manually save today's in-progress snapshot."""
    try:
        from app.manager.focus_daily_snapshot import build_snapshot, save_snapshot
        from app.manager.trade_journal import JOURNAL_PATH
        from dataclasses import asdict
        import datetime as _dt
        import json as _json

        fm = _get_fm(request)

        # Today's reset baseline
        now_utc = _dt.datetime.now(_dt.timezone.utc)
        boundary = now_utc.replace(hour=22, minute=0, second=0, microsecond=0)
        if now_utc.hour < 22:
            boundary -= _dt.timedelta(days=1)

        # Load EXIT trades from the journal
        exits = []
        import os
        if os.path.exists(_BINANCE_FUT_JOURNAL_PATH):
            with open(_BINANCE_FUT_JOURNAL_PATH, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = _json.loads(line)
                        if rec.get("event") == "EXIT":
                            exits.append(rec)
                    except _json.JSONDecodeError:
                        continue

        snap = build_snapshot(
            exits,
            boundary.timestamp(),
            boundary.timestamp() + 86400,  # until the next reset
            asdict(fm.config),
        )
        path = save_snapshot(snap, _BINANCE_FUT_SNAP_DIR)
        return {"ok": True, "snapshot": snap, "path": path}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ── Capital Tracking (deposits/withdrawals + pure performance) ──────────────────

@router.post("/capital/initial")
def capital_set_initial(amount: float = Query(..., description="Initial capital (USDT)")):
    """Set the initial capital (once at the start)."""
    from app.manager.capital_tracker import capital_tracker
    return _bcapital().set_initial(amount)


@router.post("/capital/deposit")
def capital_deposit(
    amount: float = Query(..., description="Deposit amount (USDT)"),
    memo: str = Query("", description="Memo"),
):
    """Record a deposit."""
    from app.manager.capital_tracker import capital_tracker
    return _bcapital().deposit(amount, memo)


@router.post("/capital/withdraw")
def capital_withdraw(
    amount: float = Query(..., description="Withdrawal amount (USDT)"),
    memo: str = Query("", description="Memo"),
):
    """Record a withdrawal."""
    from app.manager.capital_tracker import capital_tracker
    return _bcapital().withdraw(amount, memo)


@router.get("/capital/performance")
def capital_performance(request: Request):
    """Query pure trading performance (adjusted for deposits/withdrawals)."""
    try:
        from app.manager.capital_tracker import capital_tracker
        from app.manager.trade_journal import journal

        fm = _get_fm(request)

        # Current Bybit balance
        equity = fm._get_available_margin() or 0

        # Total realized PnL from the journal
        summary = _bjournal().get_summary()
        trading_pnl = summary.get("combined", {}).get("total_pnl", 0)

        perf = _bcapital().get_performance(equity, trading_pnl)
        return {"ok": True, **perf}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@router.get("/capital/events")
def capital_events():
    """List of deposit/withdrawal events."""
    from app.manager.capital_tracker import capital_tracker
    events = _bcapital().get_events()
    return {"ok": True, "events": events, "count": len(events)}


@router.get("/capital/status")
def capital_status():
    """Capital-tracking status."""
    from app.manager.capital_tracker import capital_tracker
    return {"ok": True, **_bcapital().get_status()}


# ── Time Analytics (performance by weekday + time-of-day) ─────────────────────

@router.get("/analytics/by-dow")
def analytics_by_dow():
    """Performance analysis by weekday (KST)."""
    import json as _json, datetime as _dt, os
    journal_path = _BINANCE_FUT_JOURNAL_PATH
    if not os.path.exists(journal_path):
        return {"ok": True, "days": []}
    dow_names = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]
    dow = {i: {"pnl": 0.0, "trades": 0, "wins": 0} for i in range(7)}
    try:
        with open(journal_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    j = _json.loads(line)
                except Exception:
                    continue
                if j.get("event") != "EXIT":
                    continue
                ts = j.get("ts", 0)
                kst = _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc) + _dt.timedelta(hours=9)
                d = kst.weekday()
                pnl = j.get("pnl_net", 0)
                dow[d]["pnl"] += pnl
                dow[d]["trades"] += 1
                if pnl > 0:
                    dow[d]["wins"] += 1
    except Exception:
        pass
    result = []
    for i in range(7):
        d = dow[i]
        if d["trades"] == 0:
            continue
        result.append({
            "day": dow_names[i],
            "day_idx": i,
            "pnl": round(d["pnl"], 2),
            "trades": d["trades"],
            "wins": d["wins"],
            "win_rate": round(d["wins"] / d["trades"] * 100, 1) if d["trades"] else 0,
        })
    result.sort(key=lambda x: x["pnl"], reverse=True)
    return {"ok": True, "days": result}


@router.get("/analytics/by-slot")
def analytics_by_slot():
    """Performance by 4-hour slot (starting at KST 07:00)."""
    import json as _json, datetime as _dt, os
    journal_path = _BINANCE_FUT_JOURNAL_PATH
    if not os.path.exists(journal_path):
        return {"ok": True, "slots": []}
    slot_labels = ["07-11", "11-15", "15-19", "19-23", "23-03", "03-07"]
    slots = {s: {"pnl": 0.0, "trades": 0, "wins": 0} for s in slot_labels}
    try:
        with open(journal_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    j = _json.loads(line)
                except Exception:
                    continue
                if j.get("event") != "EXIT":
                    continue
                ts = j.get("ts", 0)
                kst = _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc) + _dt.timedelta(hours=9)
                h = kst.hour
                if 7 <= h < 11:
                    sl = "07-11"
                elif 11 <= h < 15:
                    sl = "11-15"
                elif 15 <= h < 19:
                    sl = "15-19"
                elif 19 <= h < 23:
                    sl = "19-23"
                elif h >= 23 or h < 3:
                    sl = "23-03"
                else:
                    sl = "03-07"
                pnl = j.get("pnl_net", 0)
                slots[sl]["pnl"] += pnl
                slots[sl]["trades"] += 1
                if pnl > 0:
                    slots[sl]["wins"] += 1
    except Exception:
        pass
    result = []
    for sl in slot_labels:
        d = slots[sl]
        if d["trades"] == 0:
            continue
        result.append({
            "slot": sl,
            "pnl": round(d["pnl"], 2),
            "trades": d["trades"],
            "wins": d["wins"],
            "win_rate": round(d["wins"] / d["trades"] * 100, 1) if d["trades"] else 0,
        })
    return {"ok": True, "slots": result}


# ════════════════════════════════════════════════════════════════
# This agent's gift in return 🎁 — Weekly Intelligence / Coin Report Card /
#   Correlation Guard / Drawdown Shield / Twin Battle
# ════════════════════════════════════════════════════════════════

# ── Weekly Intelligence ─────────────────────────────────────────

@router.get("/weekly-report")
def weekly_report_current():
    """Current weekly intelligence report. ★ [2026-06-23] the weekly_intelligence module reads the global (Bybit) snapshot/report
    directory → to avoid mixing in Bybit data, return empty in the Binance window (to be isolated separately once trades/snapshots accumulate)."""
    return {"ok": False, "message": "Binance weekly report — generated once trades/snapshots accumulate (no data yet)",
            "exchange": "binance_futures"}


@router.get("/weekly-report/{week}")
def weekly_report_by_week(week: str):
    """Query a specific weekly report. (Binance — empty to isolate from the global Bybit report)"""
    return {"ok": False, "message": f"No Binance {week} report (generated once trades accumulate)"}


@router.get("/weekly-reports")
def weekly_report_all():
    """List of all weekly reports. (Binance — empty list, global Bybit report not exposed)"""
    return {"ok": True, "reports": []}


@router.post("/weekly-report/generate")
def weekly_report_generate(force: bool = Query(False)):
    """Manually generate last week's report. (Binance — held off since the global module is based on Bybit snapshots)"""
    return {"ok": False, "message": "Binance weekly report generation will be supported after per-exchange isolation"}


# ── Coin Report Card ────────────────────────────────────────────

@router.get("/coin-grades")
def coin_grades():
    """Per-coin report card — grade + score + stats."""
    from app.manager.coin_report_card import coin_report_card
    return {"ok": True, **_bcoincard().get_full_report()}


@router.get("/coin-grades/{coin}")
def coin_grade_detail(coin: str):
    """Detail of a specific coin's report."""
    from app.manager.coin_report_card import coin_report_card
    report = _bcoincard().get_full_report()
    coin_upper = coin.upper()
    if coin_upper not in report.get("coins", {}):
        return {"ok": False, "message": f"No data for {coin_upper}"}
    return {"ok": True, "coin": coin_upper, **report["coins"][coin_upper]}


@router.post("/coin-grades/refresh")
def coin_grades_refresh(days: int = Query(7, ge=1, le=90, description="Analysis period (days)")):
    """Refresh the coin report card."""
    from app.manager.coin_report_card import CoinReportCard
    card = _bcoincard(lookback_days=days)
    result = card.refresh()
    return {"ok": True, **result}


# ── Correlation Guard ───────────────────────────────────────────

@router.get("/correlation/check")
def correlation_check(
    coin: str = Query(..., description="Coin to enter (e.g. ETHUSDT)"),
    direction: str = Query(..., description="LONG or SHORT"),
    request: Request = None,
):
    """Check the correlation penalty when entering a new coin."""
    from app.manager.correlation_guard import correlation_guard
    fm = _get_fm(request)
    positions = [{"market": p.market, "direction": p.direction} for p in fm.positions]
    result = correlation_guard.check_entry(coin.upper(), direction.upper(), positions)
    return {"ok": True, **result}


@router.get("/correlation/exposure")
def correlation_exposure(request: Request):
    """Correlation exposure of current positions."""
    from app.manager.correlation_guard import correlation_guard
    fm = _get_fm(request)
    positions = [{"market": p.market, "direction": p.direction} for p in fm.positions]
    return {"ok": True, "exposure": correlation_guard.get_exposure_map(positions)}


@router.get("/correlation/matrix")
def correlation_matrix():
    """Correlation matrix (static + dynamic)."""
    from app.manager.correlation_guard import correlation_guard
    return {"ok": True, **correlation_guard.get_correlation_matrix()}


# ── Drawdown Shield ─────────────────────────────────────────────

@router.get("/drawdown/status")
def drawdown_status():
    """Current drawdown-shield status."""
    from app.manager.drawdown_shield import drawdown_shield
    return {"ok": True, **drawdown_shield.get_status()}


@router.get("/drawdown/history")
def drawdown_history():
    """Daily drawdown records."""
    from app.manager.drawdown_shield import drawdown_shield
    return {"ok": True, "history": drawdown_shield.get_history()}


@router.post("/drawdown/update")
def drawdown_update(
    current_equity: float = Query(..., description="Current equity (USDT)"),
    today_pnl: float = Query(0, description="Today's realized PnL"),
):
    """Manually update the drawdown shield.

    [2026-04-18] Refactored from PnL-based → Equity-based.
    The first argument changed from current_pnl → current_equity.
    """
    from app.manager.drawdown_shield import drawdown_shield
    result = drawdown_shield.update(current_equity=current_equity, realized_pnl_today=today_pnl)
    return {"ok": True, **result}


@router.post("/drawdown/reset-cumulative")
def drawdown_reset_cumulative():
    """Manually reset the cumulative watermark — for admins.

    Used after a long pause/restart or on a capital change (deposit/withdrawal).
    Resets max_drawdown_pct/amount too for a clean restart.
    """
    from app.manager.drawdown_shield import drawdown_shield
    drawdown_shield.reset_cumulative()
    return {"ok": True, **drawdown_shield.get_status()}


@router.post("/drawdown/reset-daily")
def drawdown_reset_daily():
    """Manually reset daily peak/current/drawdown — Phase G (2026-04-20 this agent's diagnosis).

    A bug where reset_daily() wasn't called left daily_peak_pnl stuck forever,
    for emergency relief of the CRISIS penalty -3 lingering even when the market improves.
    From 07:00 KST tomorrow on, _maybe_reset_daily_counters is called automatically.
    """
    from app.manager.drawdown_shield import drawdown_shield
    drawdown_shield.reset_daily()
    return {"ok": True, **drawdown_shield.get_status()}


# ── Twin Battle ─────────────────────────────────────────────────

@router.get("/twin/export")
def twin_export():
    """Export a standard snapshot for the sibling battle."""
    from app.manager.twin_battle import twin_battle
    return {"ok": True, **twin_battle.export_snapshot()}


@router.post("/twin/compare")
def twin_compare(request: Request):
    """Compare against the other server's snapshot. Pass the other side's export data in the body."""
    import json as _json
    from app.manager.twin_battle import twin_battle
    try:
        body = _json.loads(request._receive.__self__._body.decode() if hasattr(request, '_receive') else '{}')
    except Exception:
        body = {}
    if not body:
        return {"ok": False, "message": "Please pass the other server's snapshot data in the body"}
    result = twin_battle.compare(body)
    return {"ok": True, **result}


@router.post("/twin/name")
def twin_set_name(name: str = Query(..., description="Server name (e.g. server-a, server-b)")):
    """Set the server name."""
    from app.manager.twin_battle import twin_battle
    twin_battle.set_server_name(name)
    return {"ok": True, "server_name": name}


@router.get("/twin/status")
def twin_status():
    """Twin Battle status."""
    from app.manager.twin_battle import twin_battle
    return {"ok": True, **twin_battle.get_status()}


# ── Phase K Layer 3 — Regime Transition Watch UI ───────────────
# A permanent detector regardless of auto-trading (owner insight 2026-04-21).
# When paper_mode=True, only logs to JSONL, no real entry.
# The UI uses these records as a lens to assist manual trading.
# This agent's letter #10 α conditional deployment (2026-04-21 20:10 KST).

# ── Phase L (2026-04-22) — S3 Fee-Aware Gate UI/Promotion ──────
# This agent's letter #11 review criteria #6/#8/#10 + auto-judgment of the 7 promotion conditions
# Owner: ① D paper_mode + ② A FOCUS port + ③ A 1-week paper

@router.get("/s3-gate/promotion-status")
def s3_gate_promotion_status(request: Request):
    """Auto-judge S3 Gate eligibility for promotion to enabled=True based on 7 days of paper data.

    This agent's letter #11 section 4 promotion: 4 conditions (Track A), AND-checked:
      1. At least 10 skips triggered over 7 days of paper
      2. Virtual net_saved ≥ $20
      3. Real-entry net_vs_fee ≥ 0.5x
      4. 0 bypasses across LINK's 5 paths

    Track B (Harpoon)'s additional 3 conditions are a separate endpoint after 30 days.
    """
    import os, json, time
    fm = _get_fm(request)
    cfg = fm.config

    stats_path = os.path.join("runtime", "s3_gate_stats.json")
    summary = {}
    if os.path.exists(stats_path):
        try:
            with open(stats_path, "r", encoding="utf-8") as f:
                summary = json.load(f) or {}
        except Exception:
            summary = {}

    totals = summary.get("totals", {"checks": 0, "passed": 0, "blocked": 0,
                                    "paper_skips": 0, "live_blocks": 0})
    virtual_net_saved = float(summary.get("virtual_net_saved", 0.0))
    link_bypass_count = int(summary.get("link_bypass_count", 0))
    first_ts = float(summary.get("first_ts") or 0)
    last_ts = float(summary.get("last_ts") or 0)
    age_days = (last_ts - first_ts) / 86400.0 if first_ts > 0 and last_ts > first_ts else 0.0

    # ★ Compute cond_3 real_net_vs_fee (immediate implementation of this agent's PASS letter 4-2)
    # sum(pnl_net) / sum(fee) of FOCUS EXITs over the last 7 days. ≥ 0.5x = pass
    # Insufficient data (fewer than 5 trades) → None → conservatively False
    real_net_vs_fee = None
    real_net_vs_fee_data = {"trades": 0, "sum_gross": 0.0, "sum_fee": 0.0, "window_days": 7.0}
    try:
        from app.manager.trade_journal import journal as _j
        _resp = _bjournal().get_trades(limit=500, strategy="FOCUS", market="", include_blocked=False, page=1) or {}
        _trades = _resp.get("trades", [])
        _cutoff_ts = time.time() - 7 * 86400.0
        _sum_gross = 0.0
        _sum_fee = 0.0
        _n = 0
        for _t in _trades:
            if _t.get("event") != "EXIT":
                continue
            _ts = float(_t.get("ts", 0) or 0)
            if _ts < _cutoff_ts:
                continue
            _g = float(_t.get("pnl_gross", 0) or 0)
            _f = float(_t.get("fee", 0) or 0)
            _sum_gross += _g
            _sum_fee += abs(_f)  # normalize fee to positive
            _n += 1
        real_net_vs_fee_data["trades"] = _n
        real_net_vs_fee_data["sum_gross"] = round(_sum_gross, 2)
        real_net_vs_fee_data["sum_fee"] = round(_sum_fee, 2)
        if _n >= 5 and _sum_fee > 0:
            # ★ Definition A fix (2026-04-22 21:35, contradiction found in the first paper response):
            #   The 0.16x in this agent's comparison table v2 = 115.73 / 726.67 = gross/fee (definition A)
            #   The sibling's v2 code was (gross-fee)/fee (definition B) — mismatched with this agent's data
            #   Immediate fix: unify on gross / fee
            real_net_vs_fee = round(_sum_gross / _sum_fee, 4)
            # Definition: net_vs_fee = gross / fee.
            #   0.16x = this agent's 9-day baseline (16 cents gross per $1 fee, a loss)
            #   0.5x  = threshold (50 cents gross per $1 fee, still a loss but half recovered)
            #   1.0x  = fee = gross (break-even)
            #   1.5x+ = profitable (gross > fee × 1.5, stable profit)
    except Exception:
        pass  # keep None if journal access fails (safe)

    cond_1_min_skips = totals.get("paper_skips", 0) >= 10
    cond_2_net_saved = virtual_net_saved >= 20.0
    cond_3_real_net_vs_fee = (real_net_vs_fee is not None and real_net_vs_fee >= 0.5)
    cond_4_link_bypass = link_bypass_count == 0

    conditions = {
        "cond_1_min_skips":         {"value": cond_1_min_skips, "actual": totals.get("paper_skips", 0), "target": 10},
        "cond_2_net_saved":         {"value": cond_2_net_saved, "actual": round(virtual_net_saved, 2), "target": 20.0},
        "cond_3_real_net_vs_fee":   {"value": cond_3_real_net_vs_fee, "actual": real_net_vs_fee, "target": 0.5,
                                     "data": real_net_vs_fee_data,
                                     "note": "sum_gross / sum_fee of FOCUS EXITs over the last 7 days (definition A, matches this agent's comparison table v2). Computed when ≥5 trades + fee>0"},
        "cond_4_link_bypass":       {"value": cond_4_link_bypass, "actual": link_bypass_count, "target": 0},
    }

    all_passed = all(c["value"] for c in conditions.values())

    return {
        "ok": True,
        "generated_at": int(time.time()),
        "config": {
            "enabled": bool(getattr(cfg, "s3_gate_enabled", False)),
            "paper_mode": bool(getattr(cfg, "s3_gate_paper_mode", True)),
            "min_net_ev_usdt": float(getattr(cfg, "s3_gate_min_net_ev_usdt", 0.0)),
            "fee_multiplier": float(getattr(cfg, "s3_gate_fee_multiplier", 2.0)),
            "slippage_bps": float(getattr(cfg, "s3_gate_slippage_bps", 5.0)),
            "link_multiplier": float(getattr(cfg, "s3_gate_link_multiplier", 1.3)),
        },
        "stats": {
            "totals": totals,
            "virtual_net_saved": round(virtual_net_saved, 2),
            "link_bypass_count": link_bypass_count,
            "data_age_days": round(age_days, 2),
            "first_ts": int(first_ts) if first_ts else None,
            "last_ts": int(last_ts) if last_ts else None,
        },
        "promotion_conditions": conditions,
        "ready_for_live": all_passed,
        "disclaimer": "Track A's 4 conditions are auto-judged — Track B (Harpoon)'s additional 3 are separate. Per this agent's letter #11.",
    }


@router.get("/day-direction")
def day_direction_status(request: Request):
    """[2026-05-21 owner] Today's Day Direction state — decided daily at 09:00 KST.

    Returns:
      {"ok": True, "day_direction": "LONG"/"SHORT"/"NEUTRAL",
       "date": "YYYY-MM-DD", "reason": "...", "conv_delta": 5.0}
    """
    fm = _get_fm(request)
    return {
        "ok": True,
        "day_direction": getattr(fm, "day_direction", "NEUTRAL"),
        "date": getattr(fm, "day_direction_date", ""),
        "reason": getattr(fm, "day_direction_reason", ""),
        "conv_delta": float(getattr(fm.config, "day_direction_conv_delta", 5.0)),
        "enabled": bool(getattr(fm.config, "day_direction_enabled", True)),
        "target_hour_kst": float(getattr(fm.config, "day_direction_hour_kst", 9.0)),
        # [2026-05-23 owner] The 09:00 H4 daily range baseline
        "h4_atr_pct": float(getattr(fm, "day_h4_atr_pct", 0.0)),
        "tp1_expected_pct": float(getattr(fm, "day_tp1_expected_pct", 0.0)),
        "tp2_expected_pct": float(getattr(fm, "day_tp2_expected_pct", 0.0)),
    }


@router.get("/h4-pa-snapshot")
def h4_pa_snapshot_status(request: Request):
    """[2026-05-21 owner] Recent H4 PA Snapshot — coin state every 4 hours (1/5/9/13/17/21 KST).

    Returns:
      {"ok": True, "last_hour_kst": int, "ts": float,
       "snapshot": {market: {trend, pa}}, "strong": [...]}
    """
    fm = _get_fm(request)
    snap = getattr(fm, "h4_pa_snapshot", {}) or {}
    strong = [
        {"market": m, "trend": d.get("trend", "?"), "pa": d.get("pa", "none")}
        for m, d in snap.items()
        if d.get("pa", "none") != "none"
    ]
    return {
        "ok": True,
        "last_hour_kst": int(getattr(fm, "h4_pa_snapshot_last_hour", -1)),
        "ts": float(getattr(fm, "h4_pa_snapshot_ts", 0.0)),
        "snapshot": snap,
        "strong": strong,
        "enabled": bool(getattr(fm.config, "h4_pa_snapshot_enabled", True)),
        "hours_kst": str(getattr(fm.config, "h4_pa_snapshot_hours_kst", "1,5,9,13,17,21")),
    }


@router.get("/phase-k/recent")
def phase_k_recent(request: Request, hours: float = 6.0):
    """Phase K recent detection records + current market state (for the empty-card text).

    Returns:
      {
        "ok": True,
        "k_status": { enabled, paper_mode, btc_regime, btc_ema_gap_pct,
                      btc_regime_age_hours, adx_slope_check_enabled },
        "recent_detections": [...],  // dedupe to the latest one per coin
        "disclaimer": "Experimental signal · not an entry recommendation · accuracy disclosed after 1 week of paper",
      }
    """
    import os, json, time
    fm = _get_fm(request)
    cfg = fm.config
    now_ts = time.time()
    cutoff = now_ts - max(0.1, float(hours)) * 3600.0

    # k_status — current state info (for the empty-card text)
    b11_state = getattr(fm, "_b11_regime_state", ("", 0.0))
    b11_regime = b11_state[0] if b11_state and b11_state[0] else ""
    b11_ts = b11_state[1] if b11_state and len(b11_state) > 1 else 0.0
    age_hours = (now_ts - b11_ts) / 3600.0 if b11_ts > 0 else 0.0
    btc_gap = getattr(fm, "_btc_ema_gap_pct", None)

    k_status = {
        "enabled": bool(getattr(cfg, "regime_transition_enabled", False)),
        "paper_mode": bool(getattr(cfg, "regime_transition_paper_mode", True)),
        "btc_regime": b11_regime or "?",
        "btc_ema_gap_pct": round(btc_gap, 3) if btc_gap is not None else None,
        "btc_regime_age_hours": round(age_hours, 1),
        "ema_gap_threshold_pct": float(getattr(cfg, "regime_transition_ema_gap_threshold_pct", 0.3)),
        "min_conviction": float(getattr(cfg, "regime_transition_min_conviction", 80.0)),  # [2026-05-17 100-scale ×10] 8→80, int→float
        "min_regime_age_min": float(getattr(cfg, "regime_transition_last_change_age_min", 180.0)),
        "adx_slope_check_enabled": bool(getattr(cfg, "adx_slope_check_enabled", True)),
    }

    # phase_k_paper_log.jsonl tail + dedupe
    log_path = os.path.join("runtime", "phase_k_paper_log.jsonl")
    detections_by_coin = {}  # market → latest entry
    count_today = {}         # market → count within window
    if os.path.exists(log_path):
        try:
            # Simple tail — assumes the file is small (a 24h rolling log, under a MB)
            with open(log_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        e = json.loads(line)
                    except Exception:
                        continue
                    ts = float(e.get("ts", 0) or 0)
                    if ts < cutoff:
                        continue
                    mkt = e.get("market", "?")
                    # Keep the latest one
                    if mkt not in detections_by_coin or ts > detections_by_coin[mkt].get("ts", 0):
                        detections_by_coin[mkt] = e
                    # Cumulative count
                    count_today[mkt] = count_today.get(mkt, 0) + 1
        except Exception as exc:
            logger.debug("[Phase K] paper log read failed: %s", exc)

    # Sort — reverse chronological
    recent_detections = []
    for mkt, e in detections_by_coin.items():
        recent_detections.append({
            "ts": e.get("ts"),
            "market": mkt,
            "scanner_dir": e.get("scanner_dir"),
            "flip_dir": e.get("flip_dir"),
            "conviction": e.get("conviction"),
            "adx_now": e.get("adx_now"),
            "adx_past": e.get("adx_past"),
            "btc_ema_gap_pct": e.get("btc_ema_gap_pct"),
            "reason": e.get("reason", ""),
            "paper_mode": e.get("paper_mode"),
            "count_today": count_today.get(mkt, 1),
        })
    recent_detections.sort(key=lambda x: -(x.get("ts") or 0))

    return {
        "ok": True,
        "generated_at": int(now_ts),
        "window_hours": float(hours),
        "k_status": k_status,
        "recent_detections": recent_detections,
        "disclaimer": "Experimental signal · not an entry recommendation · accuracy disclosed after 1 week of paper",
    }
