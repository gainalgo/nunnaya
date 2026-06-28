# ============================================================
# Upbit FOCUS Strategy API Router (spot long_only)
# ------------------------------------------------------------
# REST endpoints for controlling SpotGazuaManager. Mirrors strategy_focus_router.
# ============================================================
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Query, Request

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/strategy/upbit_gazua", tags=["UPBIT_GAZUA"])


def _get_um(request: Request):
    """Get UpbitGazuaManager from system (create if absent)."""
    system = request.app.state.system
    um = getattr(system, "upbit_gazua_manager", None)
    if um is None:
        from app.manager.spot_gazua_manager import UpbitGazuaManager
        um = UpbitGazuaManager(system=system)
        system.upbit_gazua_manager = um
    return um


# ── Status / Config ─────────────────────────────────────────
@router.get("/status")
def upbit_focus_status(request: Request):
    return {"ok": True, **_get_um(request).get_status()}


@router.get("/config")
def upbit_focus_config_get(request: Request):
    return {"ok": True, "config": _get_um(request).get_status()["config"]}


@router.get("/config/defaults")
def upbit_focus_config_defaults():
    """Code defaults (SpotGazuaConfig dataclass) — source for v3 dashboard ↺ reset. Mirrors focus."""
    from dataclasses import asdict
    from app.manager.spot_gazua_manager import SpotGazuaConfig
    return {"ok": True, "defaults": asdict(SpotGazuaConfig())}


@router.post("/config")
def upbit_focus_config_set(
    request: Request,
    paper: Optional[bool] = Query(None),
    budget: Optional[float] = Query(None, ge=0),
    max_positions: Optional[int] = Query(None, ge=1, le=20),
    max_daily_plans: Optional[int] = Query(None, ge=1, le=999),
    risk_pct: Optional[float] = Query(None, ge=0, le=100),
    conv_sizing_enabled: Optional[bool] = Query(None, description="Sizing proportional to score (confidence). OFF=equal 1/N."),
    conv_size_floor: Optional[float] = Query(None, ge=0, le=1, description="Slot share used by a minimum-pass signal (0~1). 1=weighting OFF."),
    min_conf: Optional[float] = Query(None, ge=0, le=1),
    entry_conf_threshold: Optional[float] = Query(None, ge=0, le=1),
    primary_tf: Optional[str] = Query(None),
    top_n: Optional[int] = Query(None, ge=1, le=50),
    scan_interval_sec: Optional[float] = Query(None, ge=1),
    scan_exclude: Optional[str] = Query(None, description="Markets to exclude from scan (comma-separated, e.g. KRW-APENFT)"),
    block_warning_coins: Optional[bool] = Query(None, description="Block entry on exchange 'investment warning' coins (delisting risk) (bot+manual)."),
    block_caution_coins: Optional[bool] = Query(None, description="Block entry on exchange 'caution' coins. OFF=show badge only."),
    cooldown_sec: Optional[float] = Query(None, ge=0),
    tp1_mult: Optional[float] = Query(None, ge=0),
    tp2_mult: Optional[float] = Query(None, ge=0),
    sl_mult: Optional[float] = Query(None, ge=0),
    min_rr: Optional[float] = Query(None, ge=0),
    min_tp_distance_pct: Optional[float] = Query(None, ge=0),
    trailing_pct: Optional[float] = Query(None, ge=0),
    partial_pct: Optional[float] = Query(None, ge=0, le=100),
    stale_hold_hours: Optional[float] = Query(None, ge=0),
    use_pct_tp: Optional[bool] = Query(None),
    tp1_pct: Optional[float] = Query(None, ge=0),
    tp2_pct: Optional[float] = Query(None, ge=0),
    sl_pct: Optional[float] = Query(None, ge=0),
    longhold_enabled: Optional[bool] = Query(None, description="SL→hold-and-wait conversion (§4.2). Default OFF."),
    longhold_release_pct: Optional[float] = Query(None, ge=0, description="Hold-and-wait release threshold %. 0=ATR dynamic."),
    longhold_max_hold_hours: Optional[float] = Query(None, ge=0, description="Max hold time for hold-and-wait. 0=unlimited."),
    headroom_gate_pct: Optional[float] = Query(None, ge=0, description="Min headroom % to overhead resistance (§②). 0=OFF, blocks top-chasing."),
    atr_sl_floor_mult: Optional[float] = Query(None, ge=0, description="Min SL distance = mult×ATR (§②). 0=OFF, prevents instant stop-out."),
    overext_range_pos_pct: Optional[float] = Query(None, ge=0, le=1, description="Block late entries: position near top of 24H range↑ (e.g. 0.85). 0=OFF, no ADX exemption."),
    overext_min_move_pct: Optional[float] = Query(None, ge=0, description="Min 24H move |%| to judge as late/overextended."),
    blowoff_move_pct: Optional[float] = Query(None, ge=0, description="Block parabolic: 24H |move|≥% (e.g. 30) + chasing. 0=OFF."),
    guard_score_mode_enabled: Optional[bool] = Query(None, description="Compute/display guard_score (ADX+trend conf) (G1)."),
    guard_score_threshold: Optional[float] = Query(None, description="Min guard_score to enter. 0=gate OFF (display only)."),
    guard_score_total_cap: Optional[float] = Query(None, ge=0, description="Clamp guard_score to ±cap (suppress 80+). 0=unlimited."),
    multi_be_lock_enabled: Optional[bool] = Query(None, description="Lock SL upward by peak stage (profit-protection ratchet)."),
    multi_be_lock_stage1_pct: Optional[float] = Query(None, ge=0, description="Stage 1 peak%% → SL=BE+cushion."),
    multi_be_lock_stage2_pct: Optional[float] = Query(None, ge=0, description="Stage 2 peak%% → SL=entry+0.3%%."),
    multi_be_lock_stage3_pct: Optional[float] = Query(None, ge=0, description="Stage 3 peak%% → SL=entry+1.0%%."),
    multi_be_lock_stage4_pct: Optional[float] = Query(None, ge=0, description="Stage 4 peak%% → SL=entry+2.0%%."),
    multi_be_lock_fee_cushion_pct: Optional[float] = Query(None, ge=0, description="Fee cushion %% for BE lock."),
    multi_be_lock_atr_adaptive: Optional[bool] = Query(None, description="be_lock ATR adaptive — prevent noise BE-cuts on majors (low volatility) (Bybit churn fix). Lock arms only above noise."),
    multi_be_lock_atr_mult: Optional[float] = Query(None, ge=0, description="arming floor = ATR%% × this value."),
    be_stall_enabled: Optional[bool] = Query(None, description="be_stall (peak stall + momentum rollover take-profit cut)."),
    be_stall_sec: Optional[float] = Query(None, ge=0, description="Min seconds of peak stall (cut candidate)."),
    be_stall_max_since_peak_sec: Optional[float] = Query(None, ge=0, description="Stale cutoff seconds (no trigger on aged peak)."),
    be_stall_neutral_exit: Optional[bool] = Query(None, description="Time-cut on neutral momentum too? Default False (conservative)."),
    be_stall_rsi_strong: Optional[float] = Query(None, ge=0, le=100, description="RSI threshold for 'in our favor'."),
    be_stall_rsi_weak: Optional[float] = Query(None, ge=0, le=100, description="RSI threshold for 'against us'."),
    fee_rate_pct: Optional[float] = Query(None, ge=0, le=5, description="One-side fee rate % (each buy/sell). Round-trip=×2. Reflected in net PnL calc. 0=ignore fees (gross)."),
    manual_manage_enabled: Optional[bool] = Query(None, description="Should bot auto-manage SL/TP for manual (quick-trade) buy positions? OFF=hands-off (human harvests via close button)."),
    contrarian_enabled: Optional[bool] = Query(None, description="CONTRARIAN 2nd entry source ON/OFF. Default OFF. OFF in uptrend, only neutral/downtrend."),
    contrarian_max_positions: Optional[int] = Query(None, ge=0, le=10, description="Separate contrarian slots (isolated from FOCUS slots)."),
    contrarian_coin_up_th: Optional[float] = Query(None, ge=0, description="Contrarian entry eligibility: coin 24h move − BTC move ≥ this %% (relative strength)."),
    contrarian_coin_up_cap: Optional[float] = Query(None, ge=0, description="Block parabolic: exclude if coin 24h |move| above this %% (pump trap). 0=OFF."),
    contrarian_regime_gate: Optional[bool] = Query(None, description="True=no entry in uptrend (BTC UP) (only neutral/downtrend). False=always."),
    contrarian_budget: Optional[float] = Query(None, ge=0, description="Contrarian budget. 0=contrarian_budget_pct%% of equity / >0=fixed amount."),
    contrarian_budget_pct: Optional[float] = Query(None, ge=0, le=100, description="Ratio %% of equity when contrarian_budget=0."),
    contrarian_tp_pct: Optional[float] = Query(None, ge=0, description="Contrarian TP1 (partial take-profit) entry price +%%."),
    contrarian_tp2_pct: Optional[float] = Query(None, ge=0, description="Contrarian TP2 (full) entry price +%%."),
    contrarian_sl_pct: Optional[float] = Query(None, ge=0, description="Contrarian SL entry price -%%."),
    gap_check_enabled: Optional[bool] = Query(None, description="Gap check (copied from futures) — block if distance to overhead N-bar high < required gap (no entry under a ceiling)."),
    gap_check_min_pct: Optional[float] = Query(None, ge=0, description="Min required gap %%."),
    micro_1m_check_enabled: Optional[bool] = Query(None, description="1M timing (copied from futures) — defer entry on counter-bar / volume exhaustion / RSI overheat."),
    momentum_reversal_enabled: Optional[bool] = Query(None, description="Block strong recent 5M reversal (copied from futures) — no catching a falling knife."),
    momentum_reversal_strong_atr: Optional[float] = Query(None, ge=0, description="Strong reversal threshold (×5M ATR)."),
    raw_body_enabled: Optional[bool] = Query(None, description="raw_body (copied from futures) — block if recent 5M N-bar net energy opposes the entry."),
    momentum_deriv_enabled: Optional[bool] = Query(None, description="momentum_deriv (copied from futures) — block if 5M RSI/MACD rate-of-change accelerates against entry (both RSI and MACD)."),
    mtf_align_enabled: Optional[bool] = Query(None, description="MTF final block (copied from futures) — block if higher/short TF (240/30/15) structure is clearly opposed."),
    entry_expectation_enabled: Optional[bool] = Query(None, description="Entry expectation (shared futures util) — block if reward insufficient or risk excessive."),
    entry_expectation_min_reward_pct: Optional[float] = Query(None, ge=0, description="Block if reward < this %%."),
    entry_expectation_max_risk_pct: Optional[float] = Query(None, ge=0, description="Block if risk > this %%."),
    microtiming_5m_enabled: Optional[bool] = Query(None, description="microtiming_5m (copied from futures) — defer this tick if 5M RSI/MACD/BB inflection < 2/3."),
    microtiming_5m_min_score: Optional[int] = Query(None, ge=0, le=3, description="Min inflection score to pass (0~3)."),
):
    """Update only the explicitly sent fields (FOCUS pattern)."""
    um = _get_um(request)
    candidates = {
        "paper": paper, "budget": budget, "max_positions": max_positions,
        "max_daily_plans": max_daily_plans, "risk_pct": risk_pct,
        "conv_sizing_enabled": conv_sizing_enabled, "conv_size_floor": conv_size_floor,
        "min_conf": min_conf,
        "entry_conf_threshold": entry_conf_threshold, "primary_tf": primary_tf, "top_n": top_n,
        "scan_interval_sec": scan_interval_sec, "scan_exclude": scan_exclude, "cooldown_sec": cooldown_sec,
        "block_warning_coins": block_warning_coins, "block_caution_coins": block_caution_coins,
        "tp1_mult": tp1_mult, "tp2_mult": tp2_mult, "sl_mult": sl_mult, "min_rr": min_rr,
        "min_tp_distance_pct": min_tp_distance_pct, "trailing_pct": trailing_pct,
        "partial_pct": partial_pct, "stale_hold_hours": stale_hold_hours,
        "use_pct_tp": use_pct_tp, "tp1_pct": tp1_pct, "tp2_pct": tp2_pct, "sl_pct": sl_pct,
        "longhold_enabled": longhold_enabled, "longhold_release_pct": longhold_release_pct,
        "longhold_max_hold_hours": longhold_max_hold_hours,
        "headroom_gate_pct": headroom_gate_pct, "atr_sl_floor_mult": atr_sl_floor_mult,
        "overext_range_pos_pct": overext_range_pos_pct, "overext_min_move_pct": overext_min_move_pct,
        "blowoff_move_pct": blowoff_move_pct,
        "guard_score_mode_enabled": guard_score_mode_enabled, "guard_score_threshold": guard_score_threshold,
        "guard_score_total_cap": guard_score_total_cap,
        "multi_be_lock_enabled": multi_be_lock_enabled,
        "multi_be_lock_stage1_pct": multi_be_lock_stage1_pct, "multi_be_lock_stage2_pct": multi_be_lock_stage2_pct,
        "multi_be_lock_stage3_pct": multi_be_lock_stage3_pct, "multi_be_lock_stage4_pct": multi_be_lock_stage4_pct,
        "multi_be_lock_fee_cushion_pct": multi_be_lock_fee_cushion_pct,
        "multi_be_lock_atr_adaptive": multi_be_lock_atr_adaptive, "multi_be_lock_atr_mult": multi_be_lock_atr_mult,
        "be_stall_enabled": be_stall_enabled, "be_stall_sec": be_stall_sec,
        "be_stall_max_since_peak_sec": be_stall_max_since_peak_sec, "be_stall_neutral_exit": be_stall_neutral_exit,
        "be_stall_rsi_strong": be_stall_rsi_strong, "be_stall_rsi_weak": be_stall_rsi_weak,
        "fee_rate_pct": fee_rate_pct,
        "manual_manage_enabled": manual_manage_enabled,
        "contrarian_enabled": contrarian_enabled, "contrarian_max_positions": contrarian_max_positions,
        "contrarian_coin_up_th": contrarian_coin_up_th, "contrarian_coin_up_cap": contrarian_coin_up_cap,
        "contrarian_regime_gate": contrarian_regime_gate, "contrarian_budget": contrarian_budget,
        "contrarian_budget_pct": contrarian_budget_pct, "contrarian_tp_pct": contrarian_tp_pct,
        "contrarian_tp2_pct": contrarian_tp2_pct, "contrarian_sl_pct": contrarian_sl_pct,
        "gap_check_enabled": gap_check_enabled, "gap_check_min_pct": gap_check_min_pct,
        "micro_1m_check_enabled": micro_1m_check_enabled,
        "momentum_reversal_enabled": momentum_reversal_enabled, "momentum_reversal_strong_atr": momentum_reversal_strong_atr,
        "raw_body_enabled": raw_body_enabled, "momentum_deriv_enabled": momentum_deriv_enabled,
        "mtf_align_enabled": mtf_align_enabled, "entry_expectation_enabled": entry_expectation_enabled,
        "entry_expectation_min_reward_pct": entry_expectation_min_reward_pct,
        "entry_expectation_max_risk_pct": entry_expectation_max_risk_pct,
        "microtiming_5m_enabled": microtiming_5m_enabled, "microtiming_5m_min_score": microtiming_5m_min_score,
    }
    cfg = {k: v for k, v in candidates.items() if v is not None}
    # ★ generic: also accept the 672 mirror fields not declared above as query params.
    #   update_config coerces to dataclass types + hasattr filter → safe (unknown keys ignored).
    for _k, _v in request.query_params.items():
        if _k not in cfg:
            cfg[_k] = _v
    um.update_config(cfg)
    logger.info("[UPBIT_FOCUS_API] config set: %d fields", len(cfg))
    return {"ok": True, "config": um.get_status()["config"]}


# ── Enable / Disable ────────────────────────────────────────
@router.post("/enable")
def upbit_focus_enable(
    request: Request,
    paper: Optional[bool] = Query(None, description="paper mode. If omitted, keep current value (default True)."),
    budget: Optional[float] = Query(None, ge=0, description="Budget KRW (0=auto from available balance)."),
):
    um = _get_um(request)
    cfg: Dict[str, Any] = {"enabled": True}
    if paper is not None:
        cfg["paper"] = paper
    if budget is not None:
        cfg["budget"] = budget
    um.update_config(cfg)
    logger.info("[UPBIT_FOCUS_API] Enabled: %s", {k: v for k, v in cfg.items() if k != "enabled"} or "none")
    return {"ok": True, "enabled": True, **um.get_status()}


@router.post("/disable")
def upbit_focus_disable(request: Request):
    um = _get_um(request)
    um.update_config({"enabled": False})
    logger.info("[UPBIT_FOCUS_API] Disabled")
    return {"ok": True, "enabled": False}


# ── Scan preview ────────────────────────────────────────────
@router.get("/scan")
def upbit_focus_scan(request: Request):
    """Triple-confirmation scan preview (does not enter)."""
    um = _get_um(request)
    try:
        from app.manager.spot_focus_coin_selector import select_spot_focus_coin
        result = select_spot_focus_coin(
            um.system, um.client,
            primary_tf=um.config.primary_tf, top_n=um.config.top_n, min_conf=um.config.min_conf,
            exclude=um.config.scan_exclude,
            block_warning=um.config.block_warning_coins, block_caution=um.config.block_caution_coins,
        )
        return {"ok": True, "result": result}
    except Exception as exc:
        logger.warning("[UPBIT_FOCUS_API] scan error: %s", exc)
        return {"ok": False, "error": str(exc)}


@router.get("/scan-candidates")
def upbit_focus_scan_candidates(request: Request):
    """Candidate pipeline status — GreenPen diagnosis of top-volume markets (includes block reasons). Does not enter."""
    um = _get_um(request)
    try:
        from app.manager.spot_focus_coin_selector import scan_spot_focus_candidates
        rows = scan_spot_focus_candidates(
            um.system, um.client,
            primary_tf=um.config.primary_tf, top_n=um.config.top_n, min_conf=um.config.min_conf,
            exclude=um.config.scan_exclude, headroom_gate_pct=um.config.headroom_gate_pct,
            guard_score_mode_enabled=um.config.guard_score_mode_enabled,
            guard_score_threshold=um.config.guard_score_threshold,
            guard_score_total_cap=um.config.guard_score_total_cap,
            block_warning=um.config.block_warning_coins,
            block_caution=um.config.block_caution_coins,
        )
        # To show thresholds alongside scores — include entry decision criteria (final guard_score + conf gate).
        return {"ok": True, "candidates": rows, "thresholds": {
            "guard_score_threshold": um.config.guard_score_threshold,
            "entry_conf_threshold": um.config.entry_conf_threshold,
            "min_conf": um.config.min_conf,
        }}
    except Exception as exc:
        logger.warning("[UPBIT_FOCUS_API] scan-candidates error: %s", exc)
        return {"ok": False, "error": str(exc)}


@router.get("/scan-list")
def upbit_focus_scan_list(request: Request):
    """Top-volume candidates (Source1) preview."""
    um = _get_um(request)
    try:
        from app.manager.spot_focus_coin_selector import _source1_spot_volume
        markets = _source1_spot_volume(um.client, top_n=um.config.top_n, exclude=um.config.scan_exclude)
        return {"ok": True, "markets": markets}
    except Exception as exc:
        logger.warning("[UPBIT_FOCUS_API] scan-list error: %s", exc)
        return {"ok": False, "error": str(exc)}


# ── Public orderbook (for chart + orderbook widget) ─────────
@router.get("/score-timeline")
def upbit_focus_score_timeline(request: Request, market: str = Query(..., description="Market (e.g. KRW-WLD)"),
                               count: int = Query(60, ge=10, le=120)):
    """Historical guard_score+conf trajectory (score↔chart consistency check — did the score fire at a good spot)."""
    um = _get_um(request)
    try:
        from app.manager.spot_focus_coin_selector import score_timeline
        from app.manager.spot_guard_score import gs_weights_from_config
        rows = score_timeline(um.client, market, primary_tf=um.config.primary_tf,
                              count=count, total_cap=um.config.guard_score_total_cap,
                              weights=gs_weights_from_config(um.config))
        return {"ok": True, "market": market, "rows": rows, "thresholds": {
            "guard_score_threshold": um.config.guard_score_threshold,
            "entry_conf_threshold": um.config.entry_conf_threshold,
        }}
    except Exception as exc:
        logger.warning("[UPBIT_FOCUS_API] score-timeline %s error: %s", market, exc)
        return {"ok": False, "error": str(exc)}


@router.get("/orderbook")
def upbit_focus_orderbook(request: Request, market: str = Query(..., description="Market (e.g. KRW-BTC)"),
                          depth: int = Query(15, ge=1, le=30)):
    """Upbit public orderbook proxy (no auth needed, routed via server to avoid CORS)."""
    um = _get_um(request)
    try:
        return {"ok": True, **um.client.get_orderbook(market, depth=depth)}
    except Exception as exc:
        logger.warning("[UPBIT_FOCUS_API] orderbook %s error: %s", market, exc)
        return {"ok": False, "error": str(exc)}


# ── Trade Journal ───────────────────────────────────────────
@router.get("/journal")
def upbit_focus_journal(request: Request, limit: int = Query(100, ge=1, le=2000)):
    """Trade journal (newest first) + aggregates (cumulative/today PnL, win rate, per-day)."""
    um = _get_um(request)
    try:
        return {"ok": True, "rows": um.read_journal(limit=limit), "summary": um.journal_summary()}
    except Exception as exc:
        logger.warning("[UPBIT_FOCUS_API] journal error: %s", exc)
        return {"ok": False, "error": str(exc)}


@router.post("/journal/delete")
def upbit_focus_journal_delete(request: Request, ts: float = Query(..., description="ts of the journal record to delete (row unique key)")):
    """Delete one journal record (matched by ts). Record only — does not affect trades/positions."""
    um = _get_um(request)
    try:
        return um.delete_journal(ts)
    except Exception as exc:
        logger.warning("[UPBIT_FOCUS_API] journal delete error: %s", exc)
        return {"ok": False, "error": str(exc)}


# ── Quick trade (manual immediate market order, independent of bot management) ─────────────
@router.post("/order")
def upbit_focus_order(
    request: Request,
    market: str = Query(..., description="Market (e.g. KRW-BTC or BTC)"),
    side: str = Query(..., description="buy or sell"),
    krw: float = Query(0.0, ge=0, description="Buy amount (KRW) — amount mode"),
    qty: float = Query(0.0, ge=0, description="Sell quantity (0=entire actual holding) — amount mode"),
    pct: float = Query(0.0, ge=0, le=100, description="Ratio % — buy=available KRW×%, sell=holding×%. >0 ignores krw/qty (% mode)."),
):
    """Dashboard quick trade — immediate market order. Blocked in paper mode."""
    um = _get_um(request)
    try:
        res = um.quick_order(market, side, krw=krw, qty=qty, pct=pct)
        logger.info("[UPBIT_FOCUS_API] quick_order %s %s -> %s", market, side, res.get("ok"))
        return res
    except Exception as exc:
        logger.warning("[UPBIT_FOCUS_API] order %s %s error: %s", market, side, exc)
        return {"ok": False, "error": str(exc)}


@router.post("/force-close")
def upbit_focus_force_close(request: Request, market: str = Query(..., description="Market of the bot-managed position to force-close (e.g. KRW-KERNEL)")):
    """Force-close one bot-managed position (human harvest). Works in paper/live — unlike quick trade, paper can also be closed."""
    um = _get_um(request)
    try:
        res = um.force_close(market)
        logger.info("[UPBIT_FOCUS_API] force_close %s -> %s", market, res.get("ok"))
        return res
    except Exception as exc:
        logger.warning("[UPBIT_FOCUS_API] force_close %s error: %s", market, exc)
        return {"ok": False, "error": str(exc)}


@router.post("/release-cooldown")
def upbit_focus_release_cooldown(request: Request):
    """Manual COOLDOWN release — reset daily plan/SL limits + post-trade cooldown, then resume (owner clicks the badge)."""
    um = _get_um(request)
    try:
        res = um.release_cooldown()
        logger.info("[UPBIT_FOCUS_API] release_cooldown -> %s", res.get("ok"))
        return res
    except Exception as exc:
        logger.warning("[UPBIT_FOCUS_API] release_cooldown error: %s", exc)
        return {"ok": False, "error": str(exc)}


# ── 📊 near-miss postmortem (over-blocking detection · long-only shield) ──────────
@router.get("/near-miss")
def gazua_near_miss(request: Request):
    """Postmortem of buys that passed guard_score but were stopped by a final gate — 'return vs. block price'.
    Price ↑ after the block (a regrettable block)=over-blocking signal / flat or ↓ (a good block). Uses kline →
    bundled into get_near_miss_enriched's 25s response cache (reflects operator's exchange-Tick concern, independent of server-to-server Tick)."""
    um = _get_um(request)
    try:
        return {"ok": True, "near_miss": um.get_near_miss_enriched()}
    except Exception as exc:
        logger.warning("[GAZUA_API] near-miss error: %s", exc)
        return {"ok": False, "error": str(exc)}


# ── 🕊️ amnesty — adopt jailed coins ────────────────────
@router.get("/orphans")
def upbit_focus_orphans(request: Request):
    """List coins held on the exchange but stuck outside the bot (amnesty candidates). Info only."""
    um = _get_um(request)
    try:
        return {"ok": True, "orphans": um.list_orphans()}
    except Exception as exc:
        logger.warning("[UPBIT_FOCUS_API] orphans error: %s", exc)
        return {"ok": False, "error": str(exc)}


@router.post("/adopt")
def upbit_focus_adopt(request: Request, market: str = Query(..., description="Market of the coin to amnesty (e.g. KRW-WLD)")):
    """Amnesty — adopt only the single coin chosen by the operator into bot management (no auto-adoption)."""
    um = _get_um(request)
    try:
        res = um.adopt_orphan(market)
        logger.info("[UPBIT_FOCUS_API] 🕊️ adopt %s -> %s", market, res.get("ok"))
        return res
    except Exception as exc:
        logger.warning("[UPBIT_FOCUS_API] adopt %s error: %s", market, exc)
        return {"ok": False, "error": str(exc)}
