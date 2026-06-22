# ============================================================
# File: app/api/strategy_sniper_router.py
# Extracted from strategy_router.py — Phase 1-F (File Diet)
#
# Contains SNIPER strategy endpoints:
#   - GET  /sniper/list         — List active sniper positions
#   - POST /sniper/setup        — Setup SNIPER strategy for a market
#   - POST /sniper/stop         — Stop SNIPER strategy
#   - GET  /sniper/candidates   — Get sniper coin candidates
#   - GET  /sniper/backtest     — Backtest SNIPER strategy
#   - GET  /sniper/highlow      — Get high/low prices for SNIPER lookback
# ============================================================

from fastapi import APIRouter, Request, Query
from typing import Dict, Any, List, Optional, Tuple
import logging
import threading

from pydantic import BaseModel
from app.manager.oma_market_registry import MarketState
from app.core.hyper_price_store import price_store
from app.core.constants import BYBIT_MARKET_TICKERS, BYBIT_MARKET_KLINE
from app.core.currency import Q
from app.api.strategy_utils import (
    _to_float, _clamp_sniper_tp_sl,
    _snipers_budget_cap_by_price, _cap_snipers_budget,
    _check_manual_overflow, _generate_coin_warnings,
    SNIPER_MIN_TP_PCT, SNIPER_MIN_SL_PCT,
)
import requests
from time import time as time_now

logger = logging.getLogger(__name__)

router = APIRouter()

# ============================================================
# Endpoint-specific lock (was in strategy_router.py, needed locally)
# ============================================================
_sniper_setup_lock = threading.Lock()  # [FIX M11] setup_sniper 동시 호출 방지 (중복 포지션 위험)

# ============================================================
# SNIPER STRATEGY ENDPOINTS
# [CREATED 2026-01-30]
# ============================================================

@router.get(
    "/sniper/list",
    summary="List active sniper positions",
    responses={
        200: {"description": "List of active sniper strategy positions"},
    },
)
def sniper_list(request: Request):
    """
    Get list of markets with active SNIPER strategy.

    [2026-01-31] 다중 SNIPER 지원: sniper_id 포함하여 반환
    """
    from app.manager.sniper_position_store import sniper_store

    system = request.app.state.system

    # 가격 조회 helper 함수
    def get_price_safe(market: str) -> float:
        """price_store에서 가격 조회"""
        return price_store.get_price(market) or 0

    items = []
    try:
        # 1) sniper_store에서 저장된 포지션 조회 (다중 포지션 지원)
        stored_positions = sniper_store.get_all_as_list()
        stored_ids = {p.get("sniper_id") for p in stored_positions}

        # [2026-03-08] 고아 포지션 GC (일반 SNIPER)
        _sniper_gc_ids: List[str] = []

        for stored in stored_positions:
            sniper_id = stored.get("sniper_id", "")
            market = stored.get("market", "")

            if not market:
                continue

            ctx = system.coordinator.contexts.get(market)

            # 전략 활성 여부
            _sn_strat_enabled = False
            if ctx:
                _sn_ctrl = getattr(ctx, "controls", {}) or {}
                _sn_st = _sn_ctrl.get("strategy", {}) or {}
                _sn_strat_enabled = bool(_sn_st.get("enabled"))

            pos = {}
            pnl_amount = 0
            pnl_pct = 0
            entry_price = 0
            current_value = 0

            if ctx:
                pos = getattr(ctx, "position", None) or {}
                if pos:
                    qty = float(pos.get("qty", 0) or 0)
                    entry_price = float(pos.get("entry", 0) or pos.get("entry_price", 0) or 0)
                    current_px = get_price_safe(market) or entry_price
                    if qty > 0 and entry_price > 0:
                        current_value = qty * current_px
                        cost = qty * entry_price
                        pnl_amount = current_value - cost
                        pnl_pct = (pnl_amount / cost * 100) if cost > 0 else 0

            _has_holding = pos and float(pos.get("qty", 0) or 0) > 0

            params = stored.get("params", {}) or {}
            if isinstance(params, dict):
                params = dict(params)
                _tp, _sl = _clamp_sniper_tp_sl(
                    params.get("tp_pct", SNIPER_MIN_TP_PCT),
                    params.get("sl_pct", SNIPER_MIN_SL_PCT),
                )
                params["tp_pct"] = _tp
                params["sl_pct"] = _sl
            # SNIPER 화면은 precision_scope 슬롯만 제외한다.
            profile = str(params.get("profile") or "").strip().upper()
            source = str(params.get("source") or "").strip().lower()
            if source == "precision_scope":
                continue

            # 전략 비활성 + 잔고 없음 → 고아 포지션 제거
            if not _sn_strat_enabled and not _has_holding:
                _sniper_gc_ids.append(sniper_id)
                logger.info(f"[sniper_list GC] removing orphan: {sniper_id} ({market})")
                continue

            budget = stored.get("budget_usdt", 0)

            # SNIPER-specific meta
            sniper_meta = getattr(ctx, "sniper_meta", {}) if ctx else {}

            # 예상 이윤 계산 (최저가 매수 → 최고가 매도 기준)
            # 수수료 0.1% (매수 + 매도 = 0.2% 총 비용)
            expected_profit_usdt = 0.0
            expected_profit_pct = 0.0
            entry_low = params.get("entry_low_price", 0) or sniper_meta.get("entry_low_price", 0)
            exit_high = params.get("exit_high_price", 0) or sniper_meta.get("exit_high_price", 0)
            tp_pct = float(params.get("tp_pct", 2.0))
            current_px = get_price_safe(market)

            if budget > 0 and current_px > 0:
                # 방법 1: TP 기준 예상 이윤
                expected_profit_pct = tp_pct - 0.2  # TP% - 수수료 0.2%
                expected_profit_usdt = budget * (expected_profit_pct / 100)

            # 진입/청산 예정가격 계산
            entry_thres_pct = float(params.get("entry_trigger_pct", 0) or params.get("entry_threshold_pct", 0) or 0.3)
            exit_thres_pct = float(params.get("exit_trigger_pct", 0) or params.get("exit_threshold_pct", 0) or 0.3)
            entry_target = entry_low if entry_low > 0 else (current_px * (1 - entry_thres_pct / 100) if current_px > 0 else 0)
            exit_target = exit_high if exit_high > 0 else (current_px * (1 + exit_thres_pct / 100) if current_px > 0 else 0)

            items.append({
                "sniper_id": sniper_id,
                "market": market,
                "state": "ACTIVE",
                "strategy": "SNIPER",
                "active": True,
                "budget": budget,
                "current_price": current_px,
                "entry_target_price": round(entry_target, 0),
                "exit_target_price": round(exit_target, 0),
                "position": {
                    "qty": pos.get("qty", 0) if pos else 0,
                    "entry": entry_price,
                    "usdt": current_value,
                },
                "pnl": {
                    "amount": pnl_amount,
                    "pct": pnl_pct,
                    "value": current_value,
                },
                "expected_profit": {
                    "usdt": round(expected_profit_usdt, 0),
                    "pct": round(expected_profit_pct, 2),
                },
                "params": params,
                "last_meta": {
                    "mode": params.get("mode", "near_low"),
                    "lookback_min": params.get("lookback_min", 15),
                    "threshold_pct": params.get("threshold_pct", 0.3),
                    "expiry_min": params.get("expiry_min", 30),
                    **sniper_meta,
                },
            })

        # GC: 고아 포지션 일괄 제거
        for _rid in _sniper_gc_ids:
            try:
                sniper_store.remove_position(_rid)
            except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
                logger.warning("[strategy_sniper_router] %s: %s", 'GC: 고아 포지션 일괄 제거', exc, exc_info=True)

        # 2) OMA registry에서 SNIPER 모드인 마켓 (store에 없는 것) 추가
        oma = system.oma_registry
        for market, entry in oma._markets.items():
            ctx = system.coordinator.contexts.get(market)
            if not ctx:
                continue

            strategy_mode = str(getattr(ctx, "strategy_mode", "")).upper()
            ctrls = getattr(ctx, "controls", {}) or {}
            strat_ctrl = ctrls.get("strategy", {}) or {}
            ctrl_mode = str(strat_ctrl.get("mode", "")).upper()

            if strategy_mode in ("SNIPER", "SNIPER(S)") or ctrl_mode in ("SNIPER", "SNIPER(S)"):
                # 이미 stored에 있는지 확인 (legacy: market이 sniper_id인 경우)
                already_listed = any(
                    item["market"] == market for item in items
                )
                if already_listed:
                    continue

                pos = getattr(ctx, "position", None) or {}
                pnl_amount = 0
                pnl_pct = 0
                entry_price = 0
                current_value = 0

                if pos:
                    qty = float(pos.get("qty", 0) or 0)
                    entry_price = float(pos.get("entry", 0) or pos.get("entry_price", 0) or 0)
                    current_px = get_price_safe(market) or entry_price
                    if qty > 0 and entry_price > 0:
                        current_value = qty * current_px
                        cost = qty * entry_price
                        pnl_amount = current_value - cost
                        pnl_pct = (pnl_amount / cost * 100) if cost > 0 else 0

                params = strat_ctrl.get("params", {}) or {}
                if isinstance(params, dict):
                    params = dict(params)
                    _tp, _sl = _clamp_sniper_tp_sl(
                        params.get("tp_pct", SNIPER_MIN_TP_PCT),
                        params.get("sl_pct", SNIPER_MIN_SL_PCT),
                    )
                    params["tp_pct"] = _tp
                    params["sl_pct"] = _sl
                # SNIPER 화면은 precision_scope 슬롯만 제외한다.
                profile = str(params.get("profile") or "").strip().upper()
                source = str(params.get("source") or "").strip().lower()
                if source == "precision_scope":
                    continue
                entry_state = entry.get("state")
                entry_budget = entry.get("budget_usdt") or 0
                sniper_meta = getattr(ctx, "sniper_meta", {}) or {}

                # 예상 이윤 계산 (Legacy)
                legacy_tp_pct = float(params.get("tp_pct", 2.0))
                legacy_expected_pct = legacy_tp_pct - 0.2
                legacy_expected_usdt = entry_budget * (legacy_expected_pct / 100) if entry_budget > 0 else 0

                # 진입/청산 예정가격 계산 (Legacy)
                current_px = get_price_safe(market)
                entry_thres = float(params.get("entry_trigger_pct", 0) or params.get("threshold_pct", 0) or 0.3)
                exit_thres = float(params.get("exit_trigger_pct", 0) or params.get("threshold_pct", 0) or 0.3)
                entry_target = current_px * (1 - entry_thres / 100) if current_px > 0 else 0
                exit_target = current_px * (1 + exit_thres / 100) if current_px > 0 else 0

                items.append({
                    "sniper_id": market,  # Legacy: market을 sniper_id로 사용
                    "market": market,
                    "state": str(entry_state.value) if entry_state else "UNKNOWN",
                    "strategy": "SNIPER",
                    "active": True,
                    "budget": entry_budget,
                    "current_price": current_px,
                    "entry_target_price": round(entry_target, 0),
                    "exit_target_price": round(exit_target, 0),
                    "position": {
                        "qty": pos.get("qty", 0) if pos else 0,
                        "entry": entry_price,
                        "usdt": current_value,
                    },
                    "pnl": {
                        "amount": pnl_amount,
                        "pct": pnl_pct,
                        "value": current_value,
                    },
                    "expected_profit": {
                        "usdt": round(legacy_expected_usdt, 0),
                        "pct": round(legacy_expected_pct, 2),
                    },
                    "params": params,
                    "last_meta": {
                        "mode": params.get("mode", "near_low"),
                        "lookback_min": params.get("lookback_min", 15),
                        "threshold_pct": params.get("threshold_pct", 0.3),
                        "expiry_min": params.get("expiry_min", 30),
                        **sniper_meta,
                    },
                })
    except Exception as e:
        logger.warning("strategy_sniper_router.get_price_safe L302: %s", e)
        import logging
        logging.getLogger("strategy_router").warning(f"[sniper/list] error: {e}")

    return {
        "ok": True,
        "items": items,
        "count": len(items),
    }

class SniperSetupRequest(BaseModel):
    market: str
    profile: str = "SNIPER"  # SNIPER | SNIPERS
    side: str = "LONG"       # LONG | SHORT (Spot은 LONG만 실행, SHORT는 DOWN 프로필로 대응)
    source: str = ""         # optional tag (e.g., precision_scope)
    budget_usdt: float = 50
    auto_budget: bool = False  # True면 자동편성 예산으로 간주(캡 적용), 수동 입력은 False
    expiry_min: int = 30
    tp_pct: float = SNIPER_MIN_TP_PCT
    sl_pct: float = SNIPER_MIN_SL_PCT
    # Entry (저격 매수)
    entry_enabled: bool = True
    entry_lookback_min: int = 15
    entry_threshold_pct: float = 0.3
    # Exit (저격 매도)
    exit_enabled: bool = True
    exit_lookback_min: int = 15
    exit_threshold_pct: float = 0.3
    # 필터
    ai_gate_enabled: bool = True
    ai_min_score: float = 0.55
    rsi_entry_enabled: bool = True
    rsi_exit_enabled: bool = True
    vol_spike_enabled: bool = False
    vol_spike_mult: float = 2.0
    auto_reentry: bool = False
    atr_auto: bool = False
    time_filter_enabled: bool = False
    time_start: str = "09:00"
    time_end: str = "18:00"
    # Guards
    trail_tp: bool = False
    trail_dist_pct: float = 1.5
    use_limit: bool = False
    fallback_to_market: bool = True
    buy_now: bool = False
    hold_sell: bool = False
    cycle_mode: str = "AUTO"  # AUTO | UP | DOWN (SNIPER(s) 국면 모드)
    no_demote: bool = False   # True면 Autopilot demote/idle-longhold 대상에서 제외

    # Legacy compatibility
    budget: Optional[float] = None
    lookback_min: int = 15
    threshold_pct: float = 0.3
    mode: str = "near_low"

    @property
    def effective_budget(self) -> float:
        return self.budget if self.budget is not None else self.budget_usdt


# (_to_float, _snipers_budget_cap_by_price, _cap_snipers_budget → strategy_utils.py)

@router.post(
    "/sniper/setup",
    summary="Setup SNIPER strategy for a market",
    responses={
        200: {"description": "SNIPER strategy setup result"},
    },
)
def setup_sniper(
    request: Request,
    req: SniperSetupRequest,
):
    """
    Setup SNIPER strategy for a market.

    SNIPER strategy targets entry near N-minute low/high prices.
    - near_low mode: Buy when price is within threshold_pct of lookback_min low
    - near_high mode: Sell when price is within threshold_pct of lookback_min high

    [2026-01-31] 다중 SNIPER 지원: 고유 sniper_id 생성하여 반환
    """
    from app.manager.sniper_position_store import sniper_store, generate_sniper_id

    # [FIX M11] 동시 호출 시 중복 포지션 생성 방지
    if not _sniper_setup_lock.acquire(blocking=False):
        return {"ok": False, "error": "setup_in_progress"}

    try:
        system = request.app.state.system
        market = req.market.strip().upper()
        profile = str(req.profile or "SNIPER").strip().upper()
        if profile not in ("SNIPER", "SNIPERS"):
            profile = "SNIPER"
        side = str(req.side or "LONG").strip().upper()
        if side not in ("LONG", "SHORT"):
            side = "LONG"
        source = str(req.source or "").strip().lower()
        budget = req.effective_budget

        # Validate market
        if not Q.config.market_prefix and market.startswith(Q.config.market_prefix):
            return {"ok": False, "error": "Invalid market format"}

        # [2026-03-07] 수동 주문 슬롯 초과 체크 (+2 한도)
        # SNIPER(S) scope는 별도 경로(longshort_scope_deploy)에서 처리
        if not (profile == "SNIPERS" and source == "precision_scope"):
            overflow_check = _check_manual_overflow(system, "SNIPER", market)
            coin_warnings = _generate_coin_warnings(system, market, "SNIPER")
            if not overflow_check["allowed"]:
                return {"ok": False, "error": "slot_overflow", "detail": overflow_check["message"],
                        "overflow": overflow_check, "warnings": coin_warnings}
        else:
            overflow_check = {"allowed": True, "current": 0, "target": 0,
                              "overflow": 0, "is_overflow": False, "message": "scope_path"}
            coin_warnings = _generate_coin_warnings(system, market, "SNIPER")

        # [중첩 금지] 다른 전략에서 이미 ACTIVE로 운용 중인 코인은 SNIPER 등록 거부
        try:
            oma_state = system.oma_registry.get_state(market)
            if oma_state in (MarketState.ACTIVE, MarketState.RECOVERY):
                ctx = getattr(system.coordinator, "contexts", {}).get(market)
                if ctx:
                    _ctrls = getattr(ctx, "controls", {}) or {}
                    _strat = _ctrls.get("strategy", {}) or {}
                    if bool(_strat.get("enabled")):
                        _mode = str(_strat.get("mode") or "").strip().upper()
                        _params = _strat.get("params", {}) or {}
                        _prof = str(_params.get("profile") or "").strip().upper()
                        _src = str(_params.get("source") or "").strip().lower()
                        _is_scope = _mode in ("SNIPER", "SNIPER(S)") and _prof == "SNIPERS" and _src == "precision_scope"
                        _is_sniper = _mode in ("SNIPER", "SNIPER(S)") and not _is_scope
                        # 다른 전략(PINGPONG, GAZUA 등)이면 중첩 거부
                        if not _is_sniper and not _is_scope:
                            return {"ok": False, "error": f"cross_strategy_conflict:{_mode}", "market": market}
                        # 이미 SNIPER(s) scope에 있는 코인을 일반 SNIPER로 등록하려는 경우
                        if _is_scope and profile != "SNIPERS":
                            return {"ok": False, "error": "already_in_snipers_scope", "market": market}
        except (KeyError, AttributeError, TypeError, ValueError) as exc:
            logger.warning("[strategy_sniper_router] %s: %s", '이미 SNIPER(s) scope에 있는 코인을 일반 SNIPER로 등록하려는 경우', exc, exc_info=True)

        # SNIPER(s)/precision_scope의 자동편성 예산만 저가 코인 캡을 적용한다.
        # 수동 입력(req.auto_budget=False)은 캡을 넘어도 허용.
        if (profile == "SNIPERS" or source == "precision_scope") and bool(req.auto_budget):
            current_price = _to_float(price_store.get_price(market) or 0.0, 0.0)
            budget = _cap_snipers_budget(budget, current_price)

        # Upsert per market (same market re-setup should update, not duplicate)
        existing_positions = sniper_store.get_positions_by_market(market)
        dedup_removed = 0
        if existing_positions:
            sniper_id = str(existing_positions[0].get("sniper_id") or "").strip()
            if not sniper_id:
                sniper_id = generate_sniper_id(market)
            # remove accidental duplicates for same market
            for p in existing_positions[1:]:
                sid = str(p.get("sniper_id") or "").strip()
                if sid and sniper_store.remove_position(sid):
                    dedup_removed += 1
        else:
            sniper_id = generate_sniper_id(market)

        # Set to ACTIVE state (MarketState is already imported at module level)
        system.oma_set_market(
            market=market,
            state=MarketState.ACTIVE,
            reason=["sniper_setup_ui"],
        )

        # Set budget (SNIPER setup is idempotent: replace with requested budget)
        current_budget = system.oma_registry.get_budget_usdt(market) or 0
        new_budget = float(budget) if float(budget or 0) > 0 else float(current_budget)
        current_state = system.oma_registry.get_state(market) or MarketState.ACTIVE
        system.oma_registry.set_state(market, current_state, reason=["sniper_budget_setup"], budget_usdt=new_budget)

        cycle_mode = str(req.cycle_mode or "AUTO").upper()
        if cycle_mode not in ("AUTO", "UP", "DOWN"):
            cycle_mode = "AUTO"
        if profile == "SNIPERS" and cycle_mode == "AUTO":
            # SNIPER(s): 사이드 기준으로 사이클 모드 기본값 고정
            cycle_mode = "DOWN" if side == "SHORT" else "UP"
        tp_pct, sl_pct = _clamp_sniper_tp_sl(req.tp_pct, req.sl_pct)

        # Build params dict
        params = {
            "profile": profile,
            "side": side,
            "entry_enabled": req.entry_enabled,
            "entry_lookback_min": req.entry_lookback_min if req.entry_lookback_min != 15 else req.lookback_min,
            "entry_threshold_pct": req.entry_threshold_pct if req.entry_threshold_pct != 0.3 else req.threshold_pct,
            "exit_enabled": req.exit_enabled,
            "exit_lookback_min": req.exit_lookback_min if req.exit_lookback_min != 15 else req.lookback_min,
            "exit_threshold_pct": req.exit_threshold_pct if req.exit_threshold_pct != 0.3 else req.threshold_pct,
            "expiry_min": req.expiry_min,
            "tp_pct": tp_pct,
            "sl_pct": sl_pct,
            "trail_tp": req.trail_tp,
            "trail_dist_pct": req.trail_dist_pct,
            "ai_gate_enabled": req.ai_gate_enabled,
            "ai_min_score": req.ai_min_score,
            "rsi_entry_enabled": req.rsi_entry_enabled,
            "rsi_exit_enabled": req.rsi_exit_enabled,
            "vol_spike_enabled": req.vol_spike_enabled,
            "vol_spike_mult": req.vol_spike_mult,
            "auto_reentry": req.auto_reentry,
            "atr_auto": req.atr_auto,
            "time_filter_enabled": req.time_filter_enabled,
            "time_start": req.time_start,
            "time_end": req.time_end,
            "use_limit": req.use_limit,
            "fallback_to_market": req.fallback_to_market,
            "hold_sell": req.hold_sell,
            "cycle_mode": cycle_mode,
            "no_demote": bool(req.no_demote),
            "mode": req.mode,
            **({"source": source} if source else {}),
        }

        if profile == "SNIPERS":
            # 분리형 SNIPER(s) 기본 운영값: 반복 진입 + 보유 유지
            params["hold_sell"] = False

        # Configure strategy controls
        ctx = system.coordinator.get_context(market)
        if not ctx:
            ctx = system.coordinator.ensure_market(market)

        # Sync context capital with requested setup budget immediately.
        # Without this, engine sizing may still use stale usable_capital
        # (e.g., previous 100k), resulting in smaller probe buys.
        try:
            pos0 = getattr(ctx, "position", None) or {}
            qty0 = float(pos0.get("qty", 0.0) or 0.0)
            has_pos0 = qty0 > 0.0
        except (KeyError, AttributeError, TypeError, ValueError):
            logger.warning("strategy_sniper_router.setup_sniper L539 except", exc_info=True)
            qty0 = 0.0
            has_pos0 = False

        used_usdt = 0.0
        if has_pos0:
            try:
                entry0 = float(
                    pos0.get("avg_price", 0.0)
                    or pos0.get("entry_price", 0.0)
                    or pos0.get("entry", 0.0)
                    or 0.0
                )
                if entry0 > 0.0 and qty0 > 0.0:
                    used_usdt = float(entry0 * qty0)
            except (KeyError, AttributeError, TypeError, ValueError):
                logger.warning("strategy_sniper_router.setup_sniper L554 except", exc_info=True)
                used_usdt = 0.0

        ctx.allocated_capital = float(new_budget)
        if has_pos0:
            # Keep total budget consistent while preserving current position.
            ctx.usable_capital = max(0.0, float(new_budget) - float(used_usdt))
        else:
            # Fresh slot: make entered budget fully usable for next buy sizing.
            ctx.usable_capital = float(new_budget)
        ctx.wallet_mode = bool(getattr(system, "wallet_mode", False))

        ctx.update_controls({
            "strategy": {
                "enabled": True,
                "mode": "SNIPER",
                "params": params,
            }
        })
        ctx.strategy_mode = "SNIPER"
        system._save_context_state()

        # Save to position store with unique sniper_id
        sniper_store.save_position(sniper_id, {
            "budget_usdt": budget,
            "params": params,
        })

        # Buy now if requested (이미 포지션 보유 시 중복 매수 차단)
        buy_result = None
        if req.buy_now:
            # 중복 매수 방지: 이미 포지션 보유 중이면 skip
            pos = getattr(ctx, "position", None) or {}
            has_pos = float(pos.get("qty", 0) or 0) > 0
            if has_pos:
                buy_result = {"ok": False, "msg": "already_has_position"}
            elif hasattr(system, "order_fsm") and system.order_fsm:
                current_price = price_store.get_price(market) or 0
                ok, msg = system.order_fsm.submit_market_buy(
                    ctx=ctx,
                    market=market,
                    quote_amount=budget,
                    expected_price=current_price,
                    reason="sniper:buy_now_ui"
                )
                buy_result = {"ok": ok, "msg": str(msg)}
                # [FIX M12] LIVE 체결: position은 FSM apply_fill_buy()에서 실제 체결가 기준으로 기록됨
                # 여기서 open_position()을 호출하면 예상가로 중복 기록되어 entry price가 틀어짐
            elif not has_pos and system.trading_mode == "PAPER":
                current_price = price_store.get_price(market) or 0
                if current_price > 0:
                    ctx.open_position(entry_price=current_price, usdt_amount=budget, source="paper")
                    system.ledger.append("PAPER_BUY_NOW", market=market, price=current_price, usdt=budget)
                    system._save_context_state()
                    buy_result = {"ok": True, "msg": "paper_filled"}
                else:
                    buy_result = {"ok": False, "msg": "no_price_for_paper"}

        return {
            "ok": True,
            "market": market,
            "sniper_id": sniper_id,
            "upsert": bool(existing_positions),
            "dedup_removed": int(dedup_removed),
            "setup": req.dict(),
            "resolved_params": params,
            "buy_now_result": buy_result,
            "overflow": overflow_check,
            "warnings": coin_warnings,
        }
    except Exception as exc:
        logger.warning("strategy_sniper_router.setup_sniper L624: %s", exc)
        import traceback
        tb = traceback.format_exc()
        return {"ok": False, "error": str(exc), "traceback": tb}
    finally:
        _sniper_setup_lock.release()  # [FIX M11] 라기 수정 lock 해제

@router.post(
    "/sniper/stop",
    summary="Stop SNIPER strategy for a market or specific sniper_id",
    responses={
        200: {"description": "SNIPER strategy stopped"},
    },
)
def stop_sniper(
    request: Request,
    market: Optional[str] = Query(None, description="Market to stop (모든 SNIPER 중지)"),
    sniper_id: Optional[str] = Query(None, description="Specific sniper_id to stop (개별 SNIPER 중지)"),
    delete: bool = Query(False, description="If true, set DISABLED and stop OMA watch"),
):
    """Stop SNIPER strategy.

    [2026-01-31] 다중 SNIPER 지원:
    - sniper_id 지정: 해당 SNIPER 인스턴스만 중지
    - market만 지정: 해당 마켓의 모든 SNIPER 중지
    """
    from app.manager.sniper_position_store import sniper_store, extract_market_from_id

    try:
        system = request.app.state.system

        if not market and not sniper_id:
            return {"ok": False, "error": "market 또는 sniper_id 중 하나는 필수입니다"}

        # sniper_id가 지정된 경우: 개별 중지
        if sniper_id:
            sniper_id = sniper_id.strip()
            market = extract_market_from_id(sniper_id)

            # 해당 포지션만 제거
            removed = sniper_store.remove_position(sniper_id)

            # 해당 마켓에 다른 SNIPER가 남아있는지 확인
            remaining = sniper_store.get_positions_by_market(market)

            if not remaining:
                # 마지막 SNIPER였으면 마켓 상태도 변경
                target_state = MarketState.DISABLED if delete else MarketState.WATCH
                reason = ["sniper_delete_btn", "user_disabled"] if delete else ["sniper_stop_ui"]
                system.oma_set_market(
                    market=market,
                    state=target_state,
                    reason=reason,
                )
                # [FIX H4] 남은 포지션이 없을 때만 전략 비활성화 (이전엔 항상 비활성화)
                ctx = system.coordinator.get_context(market)
                if ctx:
                    ctx.update_controls({"strategy": {"enabled": False, "mode": ""}})
                    if hasattr(ctx, "strategy_mode"):
                        ctx.strategy_mode = ""
                    # [FIX N9] 재배포 시 stale state 방지: SNIPER 상태 변수 초기화
                    for _k, _v in {
                        "sniper_state": "IDLE", "sniper_watch_ts": 0.0, "sniper_probe_ts": 0.0,
                        "sniper_probe_price": 0.0, "sniper_probe_ratio": 0.0,
                        "sniper_peak_pct": 0.0, "sniper_active_ts": 0.0,
                        "sniper_dca_count": 0, "sniper_dca_initial_entry": 0.0,
                        "sniper_exit_count": 0, "sniper_last_exit_ts": 0.0,
                        "sniper_sl_streak": 0, "sniper_exit_ai_score": 0.0,
                        "snipers_scope_start_ts": 0.0,
                    }.items():
                        ctx.set_var(_k, _v)
                    system._save_context_state()

            return {"ok": True, "sniper_id": sniper_id, "market": market, "remaining_count": len(remaining), "state": target_state.value if not remaining else None}

        # market만 지정된 경우: 해당 마켓의 모든 SNIPER 중지
        market = market.strip().upper()

        target_state = MarketState.DISABLED if delete else MarketState.WATCH
        reason = ["sniper_delete_btn", "user_disabled"] if delete else ["sniper_stop_ui"]
        system.oma_set_market(
            market=market,
            state=target_state,
            reason=reason,
        )

        # Clear strategy mode
        ctx = system.coordinator.get_context(market)
        if ctx:
            ctx.update_controls({"strategy": {"enabled": False, "mode": ""}})
            if hasattr(ctx, "strategy_mode"):
                ctx.strategy_mode = ""
            # [FIX N9] 재배포 시 stale state 방지: SNIPER 상태 변수 초기화
            for _k, _v in {
                "sniper_state": "IDLE", "sniper_watch_ts": 0.0, "sniper_probe_ts": 0.0,
                "sniper_probe_price": 0.0, "sniper_probe_ratio": 0.0,
                "sniper_peak_pct": 0.0, "sniper_active_ts": 0.0,
                "sniper_dca_count": 0, "sniper_dca_initial_entry": 0.0,
                "sniper_exit_count": 0, "sniper_last_exit_ts": 0.0,
                "sniper_sl_streak": 0, "sniper_exit_ai_score": 0.0,
                "snipers_scope_start_ts": 0.0,
            }.items():
                ctx.set_var(_k, _v)
            system._save_context_state()

        # Remove all positions for this market
        removed_count = sniper_store.remove_positions_by_market(market)

        return {"ok": True, "market": market, "state": target_state.value, "removed_count": removed_count}
    except (KeyError, AttributeError, TypeError, ValueError) as exc:
        logger.warning("strategy_sniper_router.stop_sniper L734: %s", exc)
        return {"ok": False, "error": str(exc)}

@router.get(
    "/sniper/candidates",
    summary="Get sniper coin candidates",
    responses={
        200: {"description": "Sniper candidates near low/high prices"},
    },
)
def sniper_candidates(
    request: Request,
    mode: str = Query("near_low", description="Mode: near_low or near_high"),
    lookback_min: int = Query(15, description="Lookback period in minutes"),
    threshold_pct: float = Query(0.5, description="Threshold % from low/high"),
    top_n: int = Query(10, description="Number of candidates to return"),
):
    """
    Scan all exchange markets for sniper candidates.

    Returns coins that are currently near their N-minute low (for buy) or high (for sell).
    """
    system = request.app.state.system

    candidates = []
    try:
        # Get all markets from coordinator
        all_markets = list(system.coordinator.contexts.keys())

        for market in all_markets:
            if not Q.config.market_prefix and market.startswith(Q.config.market_prefix):
                continue

            current_price = price_store.get_price(market)
            if not current_price or current_price <= 0:
                continue

            # Get candle data for lookback period
            try:
                from app.manager.topn_selector import fetch_candles_minutes
                candles = fetch_candles_minutes(market, unit=1, count=lookback_min)
                if not candles or len(candles) < 5:
                    continue

                lows = [c.get("low_price", 0) for c in candles if c.get("low_price")]
                highs = [c.get("high_price", 0) for c in candles if c.get("high_price")]

                if not lows or not highs:
                    continue

                period_low = min(lows)
                period_high = max(highs)

                # Calculate distance from low/high
                dist_from_low_pct = ((current_price - period_low) / period_low * 100) if period_low > 0 else 999
                dist_from_high_pct = ((period_high - current_price) / period_high * 100) if period_high > 0 else 999

                # Check if within threshold
                if mode == "near_low" and dist_from_low_pct <= threshold_pct:
                    candidates.append({
                        "market": market,
                        "strategy": "SNIPER",
                        "current_price": current_price,
                        "period_low": period_low,
                        "period_high": period_high,
                        "dist_from_low_pct": round(dist_from_low_pct, 3),
                        "dist_from_high_pct": round(dist_from_high_pct, 3),
                        "mode": "near_low",
                        "signal_strength": round(threshold_pct - dist_from_low_pct, 3),
                    })
                elif mode == "near_high" and dist_from_high_pct <= threshold_pct:
                    candidates.append({
                        "market": market,
                        "strategy": "SNIPER",
                        "current_price": current_price,
                        "period_low": period_low,
                        "period_high": period_high,
                        "dist_from_low_pct": round(dist_from_low_pct, 3),
                        "dist_from_high_pct": round(dist_from_high_pct, 3),
                        "mode": "near_high",
                        "signal_strength": round(threshold_pct - dist_from_high_pct, 3),
                    })
            except (KeyError, AttributeError, TypeError, ValueError) as exc:
                logger.warning("[strategy_sniper_router] %s: %s", 'Check if within threshold except-> continue', exc, exc_info=True)
                continue

        # Sort by signal strength (closer to target = stronger)
        candidates.sort(key=lambda x: x["signal_strength"], reverse=True)
        candidates = candidates[:top_n]

    except (KeyError, AttributeError, TypeError, ValueError) as e:
        logger.warning("strategy_sniper_router.sniper_candidates L825: %s", e)
        import logging
        logging.getLogger("strategy_router").warning(f"[sniper/candidates] error: {e}")

    return {
        "ok": True,
        "mode": mode,
        "lookback_min": lookback_min,
        "threshold_pct": threshold_pct,
        "candidates": candidates,
        "count": len(candidates),
    }

# ============================================================
# SNIPER Backtest API
# ============================================================
@router.get(
    "/sniper/backtest",
    summary="Backtest SNIPER strategy",
)
def sniper_backtest(
    request: Request,
    market: str = Query(..., description="Market (e.g., BTCUSDT)"),
    entry_lookback_min: int = Query(15),
    entry_threshold_pct: float = Query(0.3),
    exit_lookback_min: int = Query(15),
    exit_threshold_pct: float = Query(0.3),
    tp_pct: float = Query(SNIPER_MIN_TP_PCT),
    sl_pct: float = Query(SNIPER_MIN_SL_PCT),
    candle_count: int = Query(200),
    candle_unit: int = Query(1),
):
    """SNIPER 전략 백테스트 실행."""
    from app.manager.sniper_backtest import run_sniper_backtest
    tp_pct, sl_pct = _clamp_sniper_tp_sl(tp_pct, sl_pct)

    result = run_sniper_backtest(
        market=market.upper(),
        entry_lookback_min=entry_lookback_min,
        entry_threshold_pct=entry_threshold_pct,
        exit_lookback_min=exit_lookback_min,
        exit_threshold_pct=exit_threshold_pct,
        tp_pct=tp_pct,
        sl_pct=sl_pct,
        candle_count=candle_count,
        candle_unit=candle_unit,
    )

    return {
        "ok": True,
        "market": market.upper(),
        "params": {
            "entry_lookback_min": entry_lookback_min,
            "entry_threshold_pct": entry_threshold_pct,
            "exit_lookback_min": exit_lookback_min,
            "exit_threshold_pct": exit_threshold_pct,
            "tp_pct": tp_pct,
            "sl_pct": sl_pct,
        },
        "result": {
            "total_trades": result.total_trades,
            "wins": result.wins,
            "losses": result.losses,
            "win_rate": result.win_rate,
            "total_profit_pct": result.total_profit_pct,
            "avg_profit_pct": result.avg_profit_pct,
            "max_drawdown_pct": result.max_drawdown_pct,
        },
        "trades": result.trades[-10:],
    }

# ============================================================
# P. SNIPER High/Low API (for dynamic estimate calculation)
# ============================================================

@router.get(
    "/sniper/highlow",
    summary="Get high/low prices for SNIPER lookback period",
    responses={
        200: {"description": "High/low price data for the specified lookback"},
    },
)
def get_sniper_highlow(
    market: str = Query(..., description="Market symbol (e.g., BTCUSDT)"),
    lookback_min: int = Query(240, ge=1, le=2880, description="Lookback period in minutes"),
):
    """
    [2026-02-02] Fetch actual high/low prices for SNIPER estimate calculation.

    Used by the frontend when lookback time is changed to recalculate
    estimated profit/loss based on actual price range.

    Returns:
        high: Highest price in lookback period
        low: Lowest price in lookback period
        current: Current price
        range_pct: (high - low) / low * 100
        distance_from_low_pct: (current - low) / low * 100
        suggested_tp_pct: Suggested TP based on range
        suggested_sl_pct: Suggested SL based on TP
    """
    from app.manager.reserved_selector import fetch_highlow_for_lookback

    market_norm = Q.normalize(market)

    with requests.Session() as sess:
        data = fetch_highlow_for_lookback(sess, market_norm, lookback_min)

    if data.get("high", 0) == 0 or data.get("low", 0) == 0:
        return {
            "ok": False,
            "market": market_norm,
            "lookback_min": lookback_min,
            "error": "Failed to fetch candle data",
        }

    range_pct = data.get("range_pct", 0.0)
    distance_from_low = data.get("distance_from_low_pct", 0.0)

    # TP/SL 자동 계산 (실제 변동폭 기반)
    if range_pct > 0:
        range_based_tp = range_pct * 0.40
        if distance_from_low < 15:
            range_based_tp = min(range_based_tp * 1.3, 8.0)
        elif distance_from_low < 30:
            range_based_tp = min(range_based_tp * 1.1, 7.0)
        suggested_tp = max(1.5, min(6.0, range_based_tp))
    else:
        suggested_tp = 2.5  # fallback

    suggested_sl = max(1.0, min(3.5, suggested_tp * 0.6))
    suggested_tp, suggested_sl = _clamp_sniper_tp_sl(suggested_tp, suggested_sl)

    return {
        "ok": True,
        "market": market_norm,
        "lookback_min": lookback_min,
        "high": data.get("high", 0),
        "low": data.get("low", 0),
        "current": data.get("current", 0),
        "range_pct": round(range_pct, 2),
        "distance_from_low_pct": round(distance_from_low, 2),
        "suggested_tp_pct": round(suggested_tp, 1),
        "suggested_sl_pct": round(suggested_sl, 1),
    }
