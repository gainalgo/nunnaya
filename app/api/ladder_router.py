
from __future__ import annotations
import logging
from fastapi import APIRouter, Request, HTTPException, Query, Body
from pydantic import BaseModel
from typing import Any, Dict, Optional
from enum import Enum

logger = logging.getLogger(__name__)


router = APIRouter(prefix="/api/ladder", tags=["ladder"])
# ------------------------------------------------------------
# 통합 액션: LADDER 종료 + 예약취소 + (시장가매도) + GAZUA 이동
# ------------------------------------------------------------

class ExitSellMode(str, Enum):
    hold = "hold"           # 잔고 유지
    market_sell = "market_sell"  # 시장가 전량매도

class ExitAndMoveIn(BaseModel):
    market: str
    sell_mode: ExitSellMode = ExitSellMode.hold

class LongHoldConfigIn(BaseModel):
    market: str
    strategy: Optional[str] = None          # GAZUA | LADDER | LIGHTNING | CONTRARIAN
    enabled: Optional[bool] = None
    target_profit_pct: Optional[float] = None
    notify_cooldown_sec: Optional[int] = None
    min_position_usdt: Optional[int] = None
    repeat: Optional[bool] = None
    stop_loss_pct: Optional[float] = None

class LongHoldDeployIn(BaseModel):
    """Deploy 요청 스키마"""
    market: str
    budget_usdt: float = 100
    strategy: str = "GAZUA"  # GAZUA | LADDER | LIGHTNING | CONTRARIAN
    params: Optional[Dict[str, Any]] = None


@router.post(
    "/exit_and_move",
    summary="LADDER 종료+예약취소+시장가매도+GAZUA 이동 (통합)",
    responses={
        200: {"description": "통합 액션 처리 결과"},
        400: {"description": "입력 오류/상태 오류"},
        500: {"description": "서버 오류"},
    },
)
def exit_and_move(request: Request, body: ExitAndMoveIn) -> Dict[str, Any]:
    """
    LADDER 전략 종료 + 예약취소 + (시장가매도) + GAZUA 이동을 한 번에 처리
    """
    mgr = _get_mgr(request)
    system = request.app.state.system
    market = body.market.upper()
    steps = []
    # 1. 전략 일시정지 (enabled=False)
    try:
        cfg = mgr.get_config(market)
        if cfg.get("enabled"):
            cfg["enabled"] = False
            mgr.save_config(cfg)
            steps.append("ladder_stopped")
        else:
            steps.append("ladder_already_stopped")
    except (KeyError, AttributeError, TypeError) as e:
        logger.warning("ladder_router.exit_and_move L69: %s", e)
        return {"ok": False, "error": "STOP_FAILED", "detail": str(e), "steps": steps}

    # 2. 예약 전체취소
    try:
        cancel_result = mgr.cancel_ladder_orders(cfg)
        steps.append("orders_cancelled")
    except (AttributeError, TypeError) as e:
        logger.warning("ladder_router.exit_and_move L76: %s", e)
        return {"ok": False, "error": "CANCEL_FAILED", "detail": str(e), "steps": steps}

    # 3. (옵션) 시장가 전량매도
    if body.sell_mode == ExitSellMode.market_sell:
        try:
            # 시장가 전량매도 (mgr.market_sell_all이 구현되어 있다고 가정)
            if hasattr(mgr, "market_sell_all"):
                sell_result = mgr.market_sell_all(market)
            else:
                # fallback: trade_client 직접 호출
                tc = getattr(system, "trade_client", None) or getattr(system, "exchange", None)
                if tc is None:
                    raise Exception("No trade_client/exchange found on system")
                balance = tc.get_balance(market)
                if balance and balance.get("total", 0) > 0:
                    sell_result = tc.market_sell(market, balance["total"])
                else:
                    sell_result = {"ok": False, "error": "NO_BALANCE"}
            steps.append("market_sold")
        except Exception as e:
            logger.warning("ladder_router.exit_and_move L96: %s", e)
            return {"ok": False, "error": "MARKET_SELL_FAILED", "detail": str(e), "steps": steps}
    else:
        steps.append("hold")

    # 3.5 OMA 상태 해제 (GAZUA 배치를 위해 슬롯 비우기)
    try:
        from app.manager.oma_market_registry import MarketState
        system.oma_set_market(market, MarketState.WATCH, reason=["ladder_exit_and_move"])
    except (KeyError, AttributeError, TypeError) as exc:
        logger.warning("[LADDER_API] 3.5 OMA 상태 해제 (GAZUA 배치를 위해 슬롯 비우기): %s", exc, exc_info=True)

    # 4. 전략 GAZUA로 이동 (Active)
    try:
        # 예산 계산: order_usdt(한 계단) * max_levels(계단 수) = 총 운영 규모 추정
        # 값이 없으면 기본 10 USDT
        unit_usdt = int(cfg.get("order_usdt") or 10)
        levels = int(cfg.get("max_levels") or 10)
        total_budget = unit_usdt * levels
        if total_budget < 50:
            total_budget = 100  # 최소 안전장치 ($100 USDT)

        deploy_body = LongHoldDeployIn(market=market, budget_usdt=total_budget, strategy="GAZUA", params={})
        
        # 직접 함수 호출 (FastAPI 내부)
        deploy_result = longhold_deploy(request, deploy_body)
        if deploy_result.get("ok"):
            steps.append("strategy_changed")
        else:
            steps.append("strategy_change_failed")
    except (KeyError, AttributeError, TypeError, ValueError) as e:
        logger.warning("ladder_router.exit_and_move L126: %s", e)
        return {"ok": False, "error": "GAZUA_MOVE_FAILED", "detail": str(e), "steps": steps}

    return {"ok": True, "market": market, "steps": steps}



# ------------------------------------------------------------
# Minimal schemas (router-level)
# - Manager가 Pydantic 모델을 갖고 있다면 그대로 반환해도 되지만,
#   MVP 단계에서는 dict 기반으로 주고받아도 충분합니다.
# ------------------------------------------------------------
class LadderConfigIn(BaseModel):
    market: str
    enabled: bool

    # ICAG parameters (replace fixed grid)
    order_usdt: int = 10
    max_levels: int = 10
    budget_usdt: float = 100

    # Optional ICAG tuning (defaults handled by ICAGConfig)
    anchor_avg_weight: Optional[float] = None
    anchor_vwap_weight: Optional[float] = None
    core_width_atr: Optional[float] = None
    expansion_width_atr: Optional[float] = None
    cut_pct: Optional[float] = None
    base_k: Optional[float] = None
    min_step_pct: Optional[float] = None
    inventory_cap_ratio: Optional[float] = None
    
    # Legacy compat (ignored by ICAG but kept for migration)
    spacing_mode: Optional[str] = None
    spacing_value: Optional[float] = None
    lower_bound: Optional[float] = None
    upper_bound: Optional[float] = None
    reseed_mode: Optional[str] = None


def _get_mgr(request: Request):
    """
    system에 ladder_manager를 붙여두는 패턴(oma_registry 등과 유사).
    초기화 위치가 아직 없으면 요청 시 lazy init.
    """
    system = request.app.state.system

    mgr = getattr(system, "ladder_manager", None)
    if mgr is not None:
        return mgr

    # Lazy init (MVP)
    # 실제 프로젝트에서는 hyper_system 초기화 시점에 붙이는 걸 권장
    try:
        from app.manager.ladder_manager import LadderManager  # path는 프로젝트에 맞게
    except (ImportError, AttributeError, TypeError) as e:
        logger.warning("ladder_router._get_mgr L180: %s", e)
        raise HTTPException(status_code=500, detail={"error": "LADDER_MANAGER_IMPORT_FAILED", "detail": str(e)})

    try:
        system.ladder_manager = LadderManager(system=system)
        return system.ladder_manager
    except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as e:
        logger.warning("ladder_router._get_mgr L186: %s", e)
        raise HTTPException(status_code=500, detail={"error": "LADDER_MANAGER_INIT_FAILED", "detail": str(e)})


def _get_grid_v3(request: Request):
    """Get or lazy-init the ICAG GridV3 engine."""
    system = request.app.state.system
    grid_v3 = getattr(system, "_ladder_grid_v3", None)
    if grid_v3 and grid_v3 is not False:
        return grid_v3
    # try init
    mgr = _get_mgr(request)
    try:
        from app.manager.ladder_grid_v3 import LadderGridV3
        grid_v3 = LadderGridV3(mgr)
        system._ladder_grid_v3 = grid_v3
        return grid_v3
    except (ImportError, AttributeError, TypeError) as e:
        logger.warning("ladder_router._get_grid_v3 L203: %s", e)
        raise HTTPException(status_code=500, detail={"error": "GRID_V3_INIT_FAILED", "detail": str(e)})


# ------------------------------------------------------------
# API: config
# ------------------------------------------------------------
@router.get(
    "/config",
    summary="Get ladder config for a market",
    responses={
        200: {"description": "Ladder configuration for the market"},
        500: {"description": "Failed to retrieve config"},
    },
)
def get_config(
    request: Request,
    market: str = Query(..., description="Market code (e.g., BTCUSDT)"),
) -> Dict[str, Any]:
    """
    Retrieve the ladder grid configuration for a specific market.
    """
    mgr = _get_mgr(request)
    try:
        return mgr.get_config(market)  # dict 형태 권장
    except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as e:
        logger.warning("ladder_router.get_config L228: %s", e)
        raise HTTPException(status_code=500, detail={"error": "GET_CONFIG_FAILED", "detail": str(e)})


@router.post(
    "/config",
    summary="Save ladder config for a market",
    responses={
        200: {"description": "Configuration saved successfully"},
        409: {"description": "Mode conflict with another strategy"},
        500: {"description": "Failed to save config"},
    },
)
def save_config(request: Request, cfg: LadderConfigIn) -> Dict[str, Any]:
    """
    Save or update the ladder grid configuration for a market.

    - Validates exclusive mode (no conflict with other strategies)
    - Persists configuration to disk
    """
    mgr = _get_mgr(request)

    # 서버 상호배타 검증(필수)
    try:
        mgr.validate_exclusive_mode(cfg.market)
    except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as e:
        logger.warning("ladder_router.save_config L253: %s", e)
        raise HTTPException(status_code=409, detail={"error": "MODE_CONFLICT", "detail": str(e), "market": cfg.market})

    try:
        return mgr.save_config(cfg.model_dump())
    except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as e:
        logger.warning("ladder_router.save_config L258: %s", e)
        raise HTTPException(status_code=500, detail={"error": "SAVE_CONFIG_FAILED", "detail": str(e)})

@router.get(
    "/list",
    summary="List all ladder configurations",
    responses={
        200: {"description": "List of all ladder configurations"},
    },
)
def list_ladder_configs(request: Request) -> Dict[str, Any]:
    """
    List all saved ladder grid configurations.
    """
    mgr = _get_mgr(request)
    try:
        return {"ok": True, "items": mgr.list_configs()}
    except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as e:
        logger.warning("ladder_router.list_ladder_configs L275: %s", e)
        raise HTTPException(status_code=500, detail={"error": "LIST_CONFIGS_FAILED", "detail": str(e)})


# ------------------------------------------------------------
# ICAG v3 endpoints
# ------------------------------------------------------------
@router.get(
    "/icag/diagnostics",
    summary="ICAG 진단: 마켓별 앵커, 존, 바이어스, ATR 등",
)
def icag_diagnostics(
    request: Request,
    market: str = Query(None, description="특정 마켓 (미지정시 전체)"),
) -> Dict[str, Any]:
    grid_v3 = _get_grid_v3(request)
    mgr = _get_mgr(request)
    if market:
        return {"ok": True, **grid_v3.get_diagnostics(market.upper())}
    # Show all enabled LADDER markets (from config + OMA ACTIVE/LADDER)
    results = {}
    try:
        configs = mgr.list_configs()
        for cfg in configs:
            if not isinstance(cfg, dict):
                continue
            sym = cfg.get("market", "")
            if not sym or not cfg.get("enabled"):
                continue
            diag = grid_v3.get_diagnostics(sym)
            diag["budget_usdt"] = cfg.get("budget_usdt", 0)
            diag["order_usdt"] = cfg.get("order_usdt", 0)
            diag["max_levels"] = cfg.get("max_levels", 0)
            diag["config_enabled"] = True
            results[sym] = diag
    except (KeyError, AttributeError, TypeError) as exc:
        logger.warning("[LADDER_API] Show all enabled LADDER markets (from config + OMA ACTIVE/LADDER): %s", exc, exc_info=True)

    # OMA에서 LADDER 전략으로 ACTIVE인 마켓도 포함 (config 미등록이어도 표시)
    try:
        from app.manager.oma_market_registry import MarketState
        system = request.app.state.system
        oma = getattr(system, "oma_registry", None)
        if oma:
            snapshot = oma.snapshot()
            # snapshot은 {"active": [{"market":"...", "strategy":"...", ...}], ...} 형태
            active_list = snapshot.get("active", [])
            if isinstance(active_list, list):
                for info in active_list:
                    if not isinstance(info, dict):
                        continue
                    mk = (info.get("market") or "").upper()
                    if not mk or mk in results:
                        continue
                    strat = (info.get("strategy") or "").upper()
                    if strat != "LADDER":
                        continue

                    # [FIX 2026-03-10] controls가 LADDER가 아니면 자동 복구
                    try:
                        ctx = system.coordinator.contexts.get(mk)
                        if ctx is not None:
                            c = getattr(ctx, "controls", None) or {}
                            s = c.get("strategy", {}) if isinstance(c, dict) else {}
                            cur_mode = str(s.get("mode", "")).upper()
                            if cur_mode != "LADDER":
                                from app.manager.market_controls import apply_engine_controls
                                apply_engine_controls(system, mk, "LADDER")
                    except (KeyError, AttributeError, TypeError, ValueError) as exc:
                        logger.warning("[LADDER_API] [FIX 2026-03-10] controls가 LADDER가 아니면 자동 복구: %s", exc, exc_info=True)

                    diag = grid_v3.get_diagnostics(mk)
                    cfg = mgr.get_config(mk)
                    diag["budget_usdt"] = cfg.get("budget_usdt", 0)
                    diag["order_usdt"] = cfg.get("order_usdt", 0)
                    diag["max_levels"] = cfg.get("max_levels", 0)
                    diag["config_enabled"] = bool(cfg.get("enabled"))
                    diag["oma_only"] = True  # config 없이 OMA에서만 온 마켓 표시
                    results[mk] = diag
    except (KeyError, AttributeError, TypeError, ValueError) as exc:
        logger.warning("[LADDER_API] [FIX 2026-03-10] controls가 LADDER가 아니면 자동 복구: %s", exc, exc_info=True)

    return {"ok": True, "markets": results}


@router.post(
    "/icag/sync",
    summary="ICAG 수동 그리드 동기화",
)
def icag_manual_sync(
    request: Request,
    market: str = Query(..., description="Market code (e.g., BTCUSDT)"),
) -> Dict[str, Any]:
    grid_v3 = _get_grid_v3(request)
    try:
        result = grid_v3.poll_and_sync(market.upper())
        return {"ok": True, **result}
    except (AttributeError, TypeError, ValueError) as e:
        logger.warning("ladder_router.icag_manual_sync L372: %s", e)
        raise HTTPException(status_code=500, detail={"error": "ICAG_SYNC_FAILED", "detail": str(e)})


@router.post(
    "/icag/bootstrap",
    summary="업비트 포지션 스캔 → LADDER 자동 등록 + 현재가 중심 그리드",
)
def icag_bootstrap(
    request: Request,
    budget_usdt: float = Query(100, description="마켓당 기본 예산"),
    order_usdt: float = Query(10, description="1회 주문금액"),
    max_levels: int = Query(10, description="최대 레벨 수"),
) -> Dict[str, Any]:
    grid_v3 = _get_grid_v3(request)
    try:
        result = grid_v3.bootstrap_from_positions(
            default_budget_usdt=budget_usdt,
            default_order_usdt=order_usdt,
            default_max_levels=max_levels,
        )
        return result
    except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as e:
        logger.warning("ladder_router.icag_bootstrap L394: %s", e)
        raise HTTPException(status_code=500, detail={"error": "BOOTSTRAP_FAILED", "detail": str(e)})


@router.post(
    "/icag/cancel-all",
    summary="ICAG 마켓의 모든 주문 취소",
)
def icag_cancel_all(
    request: Request,
    market: str = Query(..., description="Market code"),
    remove: bool = Query(False, description="Also remove grid config"),
) -> Dict[str, Any]:
    grid_v3 = _get_grid_v3(request)
    mkt = market.upper()
    canceled = grid_v3._cancel_all_orders(mkt)
    removed = False
    if remove:
        # 그리드 설정에서도 제거
        try:
            if hasattr(grid_v3, 'grids') and mkt in grid_v3.grids:
                del grid_v3.grids[mkt]
                removed = True
            if hasattr(grid_v3, '_save_config'):
                grid_v3._save_config()
            elif hasattr(grid_v3, 'save_config'):
                grid_v3.save_config()
        except (KeyError, AttributeError, TypeError) as exc:
            logger.warning("[LADDER_API] 그리드 설정에서도 제거: %s", exc, exc_info=True)
    return {"ok": True, "market": mkt, "canceled": canceled, "removed": removed}


# ------------------------------------------------------------
# API: status (reconcile)
# ------------------------------------------------------------
@router.get(
    "/status",
    summary="Get ladder status for a market",
    responses={
        200: {"description": "Current ladder status with order reconciliation"},
    },
)
def get_status(
    request: Request,
    market: str = Query(..., description="Market code (e.g., BTCUSDT)"),
) -> Dict[str, Any]:
    """
    Get the current ladder grid status for a market.

    - Runs reconciliation to sync with exchange
    - Returns current order levels and fill status
    """
    mgr = _get_mgr(request)
    try:
        return mgr.reconcile(market)
    except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as e:
        logger.warning("ladder_router.get_status L449: %s", e)
        raise HTTPException(status_code=500, detail={"error": "STATUS_FAILED", "detail": str(e)})

@router.get(
    "/market_stats",
    summary="Get market statistics for ladder setup",
    responses={
        200: {"description": "Market stats including suggested max levels"},
    },
)
def market_stats(
    request: Request,
    market: str = Query(..., description="Market code (e.g., BTCUSDT)"),
    spacing_mode: str = Query("", description="Spacing mode (PERCENT or USDT)"),
    spacing_value: str = Query("", description="Spacing value for calculation"),
) -> Dict[str, Any]:
    """
    Get market statistics useful for ladder grid setup.

    - Returns current price and suggested max levels
    - Uses spacing parameters if provided
    """
    mgr = _get_mgr(request)
    try:
        # spacing 정보를 주면 suggested_max_levels 계산에 사용
        sm = spacing_mode or None
        try:
            sv = float(spacing_value) if spacing_value not in ("", None) else None
        except (TypeError, ValueError):
            logger.warning("ladder_router.market_stats L477 except", exc_info=True)
            sv = None

        return mgr.get_market_stats(market=market, spacing_mode=sm, spacing_value=sv)
    except (TypeError, ValueError) as e:
        logger.warning("ladder_router.market_stats L481: %s", e)
        raise HTTPException(status_code=500, detail={"error": "MARKET_STATS_FAILED", "detail": str(e), "market": market})

# ------------------------------------------------------------
# API: seed
# ------------------------------------------------------------
@router.post(
    "/seed",
    summary="Seed ladder buy orders",
    responses={
        200: {"description": "Ladder buy orders seeded successfully"},
        400: {"description": "Ladder disabled or no price available"},
        409: {"description": "Mode conflict with another strategy"},
    },
)
def seed_orders(
    request: Request,
    market: str = Query(..., description="Market code to seed orders for"),
) -> Dict[str, Any]:
    """
    Seed ladder grid buy orders for a market.

    - Validates exclusive mode and enabled status
    - Runs reconcile before seeding to prevent duplicates
    - Places buy limit orders at calculated price levels
    """
    mgr = _get_mgr(request)

    # 서버 상호배타 검증(필수)
    try:
        mgr.validate_exclusive_mode(market)
    except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as e:
        logger.warning("ladder_router.seed_orders L512: %s", e)
        raise HTTPException(status_code=409, detail={"error": "MODE_CONFLICT", "detail": str(e), "market": market})

    try:
        cfg = mgr.get_config(market)
    except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as e:
        logger.warning("ladder_router.seed_orders L517: %s", e)
        raise HTTPException(status_code=500, detail={"error": "GET_CONFIG_FAILED", "detail": str(e)})

    if not bool(cfg.get("enabled")):
        raise HTTPException(status_code=400, detail={"error": "LADDER_DISABLED", "market": market})

    # MVP: seed 전에 reconcile(중복 방지)
    try:
        _ = mgr.reconcile(market)
    except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError):
        logger.warning("ladder_router.seed_orders L526 except", exc_info=True)
        # reconcile 실패는 seed 자체를 막는 편이 안전하지만,
        # 프로젝트 상황에 따라 완화 가능. MVP는 막습니다.
        raise HTTPException(status_code=500, detail={"error": "RECONCILE_FAILED", "market": market})

    # 현재가 확보
    try:
        current_price = mgr.get_current_price(market)
    except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as e:
        logger.warning("ladder_router.seed_orders L534: %s", e)
        raise HTTPException(status_code=500, detail={"error": "NO_PRICE", "detail": str(e), "market": market})

    if not current_price or float(current_price) <= 0:
        raise HTTPException(status_code=400, detail={"error": "NO_PRICE", "market": market})

    # 수수료/슬리피지 기반 경고 (MVP: 경고만 반환)
    warnings = []
    try:
        warnings = mgr.compute_warnings(cfg, float(current_price))
    except (TypeError, ValueError):
        logger.warning("ladder_router.seed_orders L544 except", exc_info=True)
        warnings = []

    current_price = mgr.get_current_price(market)
    if not current_price or float(current_price) <= 0:
        # fallback: server-side stats
        st = mgr.get_market_stats(market=market)
        current_price = st.get("last_price")

    if not current_price or float(current_price) <= 0:
        raise HTTPException(status_code=400, detail={"error":"NO_PRICE","market":market})

    # seed 실행 (MVP: 매수 주문만)
    try:
        summary = mgr.seed_buy_orders(cfg, float(current_price))
    except (TypeError, ValueError) as e:
        logger.warning("ladder_router.seed_orders L559: %s", e)
        raise HTTPException(status_code=500, detail={"error": "SEED_FAILED", "detail": str(e), "market": market})

    return {
        "ok": True,
        "market": market,
        "current_price": float(current_price),
        "warnings": warnings,
        "summary": summary,
    }

# ------------------------------------------------------------
# API: cancel
# ------------------------------------------------------------

# ------------------------------------------------------------
# API: Per-step status update, edit, delete (LADDER)
# ------------------------------------------------------------
@router.post(
    "/step/status",
    summary="Update status of a ladder step (pause/resume)",
    responses={
        200: {"description": "Step status updated"},
        404: {"description": "Step not found"},
    },
)
def update_step_status(
    request: Request,
    market: str = Query(..., description="Market code (e.g., BTCUSDT)"),
    step_uuid: str = Query(..., description="UUID of the ladder step"),
    status: str = Query(..., description="New status: active|paused|deleted"),
) -> Dict[str, Any]:
    """
    Update the status of a specific ladder step (active/paused/deleted).
    """
    mgr = _get_mgr(request)
    try:
        ok = mgr.update_step_status(market, step_uuid, status)
        if not ok:
            raise HTTPException(status_code=404, detail={"error": "STEP_NOT_FOUND", "market": market, "uuid": step_uuid})
        return {"ok": True, "market": market, "uuid": step_uuid, "status": status}
    except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as e:
        logger.warning("ladder_router.update_step_status L600: %s", e)
        raise HTTPException(status_code=500, detail={"error": "STEP_STATUS_UPDATE_FAILED", "detail": str(e), "market": market, "uuid": step_uuid})

@router.post(
    "/step/edit",
    summary="Edit a ladder step (price/amount)",
    responses={
        200: {"description": "Step edited successfully"},
        404: {"description": "Step not found"},
    },
)
def edit_step(
    request: Request,
    market: str = Query(..., description="Market code (e.g., BTCUSDT)"),
    step_uuid: str = Query(..., description="UUID of the ladder step"),
    price: float = Query(None, gt=0, le=1_000_000, description="New price for the step (optional)"),
    amount: float = Query(None, gt=0, le=100_000, description="New amount for the step (optional)"),
) -> Dict[str, Any]:
    """
    Edit the price and/or amount of a specific ladder step.
    """
    mgr = _get_mgr(request)
    try:
        ok = mgr.edit_step(market, step_uuid, price=price, amount=amount)
        if not ok:
            raise HTTPException(status_code=404, detail={"error": "STEP_NOT_FOUND", "market": market, "uuid": step_uuid})
        return {"ok": True, "market": market, "uuid": step_uuid, "price": price, "amount": amount}
    except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as e:
        logger.warning("ladder_router.edit_step L627: %s", e)
        raise HTTPException(status_code=500, detail={"error": "STEP_EDIT_FAILED", "detail": str(e), "market": market, "uuid": step_uuid})

@router.post(
    "/step/delete",
    summary="Delete a ladder step",
    responses={
        200: {"description": "Step deleted successfully"},
        404: {"description": "Step not found"},
    },
)
def delete_step(
    request: Request,
    market: str = Query(..., description="Market code (e.g., BTCUSDT)"),
    step_uuid: str = Query(..., description="UUID of the ladder step"),
) -> Dict[str, Any]:
    """
    Delete a specific ladder step (marks as deleted and cancels order if open).
    """
    mgr = _get_mgr(request)
    try:
        ok = mgr.delete_step(market, step_uuid)
        if not ok:
            raise HTTPException(status_code=404, detail={"error": "STEP_NOT_FOUND", "market": market, "uuid": step_uuid})
        return {"ok": True, "market": market, "uuid": step_uuid}
    except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as e:
        logger.warning("ladder_router.delete_step L652: %s", e)
        raise HTTPException(status_code=500, detail={"error": "STEP_DELETE_FAILED", "detail": str(e), "market": market, "uuid": step_uuid})
@router.post(
    "/cancel",
    summary="Cancel all ladder orders",
    responses={
        200: {"description": "Ladder orders cancelled successfully"},
        409: {"description": "Mode conflict"},
    },
)
def cancel_orders(
    request: Request,
    market: str = Query(..., description="Market code to cancel orders for"),
) -> Dict[str, Any]:
    """
    Cancel all pending ladder grid orders for a market.

    - Validates exclusive mode
    - Cancels all open buy orders in the ladder grid
    """
    mgr = _get_mgr(request)

    # 서버 상호배타 검증(필수)
    try:
        mgr.validate_exclusive_mode(market)
    except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as e:
        logger.warning("ladder_router.cancel_orders L677: %s", e)
        raise HTTPException(status_code=409, detail={"error": "MODE_CONFLICT", "detail": str(e), "market": market})

    try:
        cfg = mgr.get_config(market)
    except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as e:
        logger.warning("ladder_router.cancel_orders L682: %s", e)
        raise HTTPException(status_code=500, detail={"error": "GET_CONFIG_FAILED", "detail": str(e)})

    try:
        summary = mgr.cancel_ladder_orders(cfg)
    except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as e:
        logger.warning("ladder_router.cancel_orders L687: %s", e)
        raise HTTPException(status_code=500, detail={"error": "CANCEL_FAILED", "detail": str(e), "market": market})

    return {"ok": True, "market": market, "summary": summary}


# ------------------------------------------------------------
# LongHold (GAZUA / LADDER / LIGHTNING / CONTRARIAN) — notify-only watchlist
# ------------------------------------------------------------

class LongHoldConfigIn(BaseModel):
    market: str
    strategy: Optional[str] = None          # GAZUA | LADDER | LIGHTNING | CONTRARIAN
    enabled: Optional[bool] = None
    target_profit_pct: Optional[float] = None
    notify_cooldown_sec: Optional[int] = None
    min_position_usdt: Optional[int] = None
    repeat: Optional[bool] = None
    stop_loss_pct: Optional[float] = None


class LongHoldDeployIn(BaseModel):
    """Deploy 요청 스키마"""
    market: str
    budget_usdt: float = 100
    strategy: str = "GAZUA"  # GAZUA | LADDER | LIGHTNING | CONTRARIAN
    params: Optional[Dict[str, Any]] = None


def _check_budget_available(system, required_usdt: float) -> tuple[bool, float, float]:
    """
    예산 가용 여부 확인
    Returns: (available, remaining_usdt, total_deployable_usdt)
    """
    try:
        equity = float(getattr(system, "_last_equity_usdt", 0) or getattr(system, "equity_usdt", 0) or 0)
        deploy_ratio = float(getattr(system, "deploy_ratio", 0.8) or 0.8)
        total_deployable = equity * deploy_ratio
        
        # 현재 배치된 금액 계산
        deployed_usdt = 0.0
        oma = getattr(system, "oma_registry", None)
        if oma and hasattr(oma, "snapshot"):
            snap = oma.snapshot()
            for item in snap.get("active", []):
                if isinstance(item, dict):
                    b_usdt = float(item.get("budget_usdt", 0) or 0)
                    strat = str(item.get("strategy") or "").upper()
                    
                    # [User Request] LADDER는 그리드 특성상 예산을 즉시 다 쓰지 않음.
                    # 따라서 장부상으로는 30%만 점유한 것으로 계산하여 '오버부킹'을 허용함.
                    if strat == "LADDER":
                        deployed_usdt += b_usdt * 0.3
                    else:
                        deployed_usdt += b_usdt
        
        remaining = total_deployable - deployed_usdt
        return remaining >= required_usdt, remaining, total_deployable
    except (KeyError, AttributeError, TypeError, ValueError) as exc:
        logger.error("[BUDGET_CHECK] budget check failed, DENYING entry: %s", exc, exc_info=True)
        return False, 0.0, 0.0  # 체크 실패 시 차단 (안전 방향)


def _notify_budget_exhausted(market: str, strategy: str, required_usdt: float, remaining_usdt: float):
    """예산 부족 시 텔레그램 알림"""
    try:
        from app.notify.telegram import send_telegram
        send_telegram(
            f"⚠️ *예산 부족 - 배치 대기*\n\n"
            f"📌 마켓: {market}\n"
            f"📊 전략: {strategy}\n"
            f"💰 필요: {required_usdt:,.2f} USDT\n"
            f"💵 잔여: {remaining_usdt:,.2f} USDT\n\n"
            f"_예산 확보 시 수동 배치 필요_"
        )
    except (AttributeError, TypeError, ValueError) as exc:
        logger.warning("[LADDER_API] ladder_router._notify_budget_exhausted fallback: %s", exc, exc_info=True)


@router.get(
    "/longhold/config",
    summary="Get LongHold config for a market",
    responses={
        200: {"description": "LongHold configuration for the market"},
    },
)
def longhold_get_config(
    request: Request,
    market: str = Query(..., description="Market code"),
) -> Dict[str, Any]:
    """
    Retrieve the LongHold (notify-only) configuration for a market.
    """
    mgr = _get_mgr(request)
    try:
        return {"ok": True, "item": mgr.get_longhold_config(market)}
    except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as e:
        logger.warning("ladder_router.longhold_get_config L783: %s", e)
        raise HTTPException(status_code=500, detail={"error": "LONGHOLD_GET_FAILED", "detail": str(e), "market": market})


@router.post(
    "/longhold/config",
    summary="Save LongHold config for a market",
    responses={
        200: {"description": "LongHold configuration saved"},
    },
)
def longhold_save_config(request: Request, cfg: LongHoldConfigIn) -> Dict[str, Any]:
    """
    Save or update the LongHold configuration for a market.

    - Configures target profit percentage and notification settings
    """
    mgr = _get_mgr(request)
    try:
        item = mgr.save_longhold_config(cfg.model_dump(exclude_none=True))
        return {"ok": True, "item": item}
    except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as e:
        logger.warning("ladder_router.longhold_save_config L804: %s", e)
        raise HTTPException(status_code=500, detail={"error": "LONGHOLD_SAVE_FAILED", "detail": str(e), "market": getattr(cfg, "market", None)})


@router.post(
    "/longhold/deploy",
    summary="Deploy a LongHold strategy with budget check",
    responses={
        200: {"description": "Strategy deployed successfully"},
        400: {"description": "Budget exhausted or validation failed"},
    },
)
def longhold_deploy(request: Request, body: LongHoldDeployIn) -> Dict[str, Any]:
    """
    Deploy a LongHold strategy (GAZUA/LADDER/LIGHTNING/CONTRARIAN).
    
    - Checks if budget is available before deploying
    - Sends Telegram notification if budget is exhausted
    - Creates OMA entry and config
    """
    system = request.app.state.system
    mgr = _get_mgr(request)
    
    market = body.market.upper()

    budget_usdt = body.budget_usdt
    strategy = body.strategy.upper()
    params = body.params or {}
    
    # 1. 이미 배치되어 있는지 확인
    try:
        oma = getattr(system, "oma_registry", None)
        if oma and hasattr(oma, "snapshot"):
            snap = oma.snapshot()
            active_markets = {
                str(x.get("market") if isinstance(x, dict) else x).upper()
                for x in snap.get("active", [])
            }
            if market in active_markets:
                return {
                    "ok": False,
                    "error": "ALREADY_DEPLOYED",
                    "detail": f"{market} is already active with another strategy",
                    "market": market
                }
    except (KeyError, AttributeError, TypeError, ValueError) as exc:
        logger.warning("[LADDER_API] 1. 이미 배치되어 있는지 확인: %s", exc, exc_info=True)
    
    # 2. 예산 체크
    available, remaining_usdt, total_deployable = _check_budget_available(system, budget_usdt)
    
    if not available:
        _notify_budget_exhausted(market, strategy, budget_usdt, remaining_usdt)
        return {
            "ok": False,
            "error": "BUDGET_EXHAUSTED",
            "detail": f"Insufficient budget: need {budget_usdt:,.2f} USDT, remaining {remaining_usdt:,.2f} USDT",
            "market": market,
            "required_usdt": budget_usdt,
            "remaining_usdt": remaining_usdt,
            "total_deployable_usdt": total_deployable
        }
    
    # 3. LongHold config 저장
    try:
        tp_pct = params.get("tp", params.get("tp_pct", 5.0))
        config_data = {
            "market": market,
            "strategy": strategy,
            "enabled": True,
            "target_profit_pct": tp_pct,
            "notify_cooldown_sec": params.get("cooldown_sec", 600),
            "min_position_usdt": 5,
        }
        mgr.save_longhold_config(config_data)
    except (KeyError, AttributeError, TypeError) as e:
        logger.warning("ladder_router.longhold_deploy L881: %s", e)
        return {"ok": False, "error": "CONFIG_SAVE_FAILED", "detail": str(e), "market": market}
    
    # 4. OMA에 ACTIVE로 등록
    try:
        from app.manager.oma_market_registry import MarketState
        reason = [f"longhold_deploy:{strategy}", f"budget:{budget_usdt}"]
        system.oma_set_market(market, MarketState.ACTIVE, reason=reason, budget_usdt=budget_usdt)
    except (KeyError, AttributeError, TypeError, ValueError) as e:
        logger.warning("ladder_router.longhold_deploy L889: %s", e)
        return {"ok": False, "error": "OMA_SET_FAILED", "detail": str(e), "market": market}
    
    # 5. [2026-02-01] context_state에 전략 설정 등록 (GAZUA 조건 추가)
    # LongHold 코인은 user_sell_only=True로 자동매매에서 완전 제외
    try:
        from app.manager.market_controls import apply_engine_controls
        apply_engine_controls(system, market, strategy)
        
        ctx = system.coordinator.ensure_market(market)
        
        # LongHold용 특수 설정: 자동매매 완전 비활성화
        # - user_sell_only=True: TP/SL 자동 매도 비활성화 (사용자만 매도 가능)
        # - sl=-50: 사실상 SL 비활성화 (장기 보유 의도 존중)
        longhold_params = {
            "tp": tp_pct,
            "sl": params.get("sl", -50.0),  # LongHold 기본 SL: -50%
            "user_sell_only": True,  # [CRITICAL] 자동매매 완전 제외
            "hold_sell": False,
            "buy_now": params.get("buy_now", False),
        }
        
        patch = {"strategy": {"params": longhold_params}}
        ctx.update_controls(patch)
        system._save_context_state()
    except (KeyError, AttributeError, TypeError) as e:
        # 전략 설정 실패해도 OMA 등록은 성공했으므로 경고만
        import logging
        logging.warning(f"LongHold strategy setup warning for {market}: {e}")
    
    # 6. 성공 알림
    try:
        from app.notify.telegram import send_telegram
        send_telegram(
            f"🚀 *{strategy} 배치 완료*\n\n"
            f"📌 마켓: {market}\n"
            f"💰 예산: {budget_usdt:,.2f} USDT\n"
            f"🎯 TP: {params.get('tp', 5.0)}%"
        )
    except (KeyError, AttributeError, TypeError, ValueError) as exc:
        logger.warning("[LADDER_API] 6. 성공 알림: %s", exc, exc_info=True)
    
    return {
        "ok": True,
        "market": market,
        "strategy": strategy,
        "budget_usdt": budget_usdt,
        "params": params,
        "remaining_usdt": remaining_usdt - budget_usdt
    }


@router.post(
    "/longhold/stop",
    summary="Stop a LongHold strategy",
    responses={
        200: {"description": "Strategy stopped successfully"},
    },
)
def longhold_stop(request: Request, market: str = Query(...), action: str = Query("stop")) -> Dict[str, Any]:
    """
    Stop or delete a LongHold strategy.
    
    - action='stop': Disable config but keep position
    - action='delete': Remove config and demote from OMA
    """
    system = request.app.state.system
    mgr = _get_mgr(request)
    
    market = market.upper()

    try:
        if action == "delete":
            # OMA에서 제거
            try:
                from app.manager.oma_market_registry import MarketState
                system.oma_set_market(market, MarketState.DISABLED, reason=["longhold_delete"])
            except (KeyError, AttributeError, TypeError) as exc:
                logger.warning("[LADDER_API] OMA에서 제거: %s", exc, exc_info=True)
            # Config 제거
            mgr.remove_longhold_config(market)
            return {"ok": True, "market": market, "action": "deleted"}
        else:
            # Config만 비활성화
            cfg = mgr.get_longhold_config(market) or {}
            cfg["enabled"] = False
            mgr.save_longhold_config(cfg)
            return {"ok": True, "market": market, "action": "stopped"}
    except (KeyError, AttributeError, TypeError) as e:
        logger.warning("ladder_router.longhold_stop L979: %s", e)
        raise HTTPException(status_code=500, detail={"error": "LONGHOLD_STOP_FAILED", "detail": str(e), "market": market})


@router.post(
    "/longhold/update",
    summary="Update LongHold strategy params",
    responses={
        200: {"description": "Strategy updated successfully"},
    },
)
def longhold_update(request: Request, body: LongHoldDeployIn) -> Dict[str, Any]:
    """
    Update an existing LongHold strategy parameters.
    """
    mgr = _get_mgr(request)
    
    market = body.market.upper()

    params = body.params or {}
    
    try:
        cfg = mgr.get_longhold_config(market) or {"market": market}
        cfg["strategy"] = body.strategy.upper()
        if "tp" in params or "tp_pct" in params:
            cfg["target_profit_pct"] = params.get("tp", params.get("tp_pct", cfg.get("target_profit_pct", 5.0)))
        if "cooldown_sec" in params:
            cfg["notify_cooldown_sec"] = params["cooldown_sec"]
        
        mgr.save_longhold_config(cfg)
        return {"ok": True, "market": market, "config": cfg}
    except (KeyError, AttributeError, TypeError, ValueError) as e:
        logger.warning("ladder_router.longhold_update L1012: %s", e)
        raise HTTPException(status_code=500, detail={"error": "LONGHOLD_UPDATE_FAILED", "detail": str(e), "market": market})


@router.get(
    "/longhold/list",
    summary="List all LongHold configurations",
    responses={
        200: {"description": "List of all LongHold configurations"},
    },
)
def longhold_list(request: Request) -> Dict[str, Any]:
    """
    List all saved LongHold configurations.
    """
    mgr = _get_mgr(request)
    try:
        return {"ok": True, "items": mgr.list_longhold_configs()}
    except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as e:
        logger.warning("ladder_router.longhold_list L1030: %s", e)
        raise HTTPException(status_code=500, detail={"error": "LONGHOLD_LIST_FAILED", "detail": str(e)})


@router.get(
    "/longhold/candidates",
    summary="Scan for LongHold candidates",
    responses={
        200: {"description": "List of candidate markets for LongHold"},
    },
)
def longhold_candidates(
    request: Request,
    strategy: str = Query("GAZUA", description="Strategy type (GAZUA or LADDER)"),
    n: int = Query(3, ge=1, le=50, description="Number of candidates to return"),
    method: str = Query("candles", description="Scan method (candles or ticker)"),
    candle_unit_minutes: int = Query(5, ge=1, le=240, description="Candle timeframe"),
    candle_count: int = Query(200, ge=50, le=500, description="Number of candles"),
    seconds: int = Query(180, ge=30, le=600, description="Scan duration for ticker method"),
    interval_sec: float = Query(1.0, ge=0.2, le=5.0, description="Poll interval"),
    chunk_size: int = Query(100, ge=20, le=200, description="Markets per batch"),
    max_markets: Optional[int] = Query(None, ge=10, le=400, description="Max markets to scan"),
) -> Dict[str, Any]:
    """
    Scan markets to find promising LongHold candidates.

    - Uses candle or ticker data for analysis
    - Returns top N candidates based on strategy criteria
    """
    mgr = _get_mgr(request)
    try:
        return mgr.scan_longhold_candidates(
            strategy=strategy,
            n=int(n),
            method=method,
            candle_unit_minutes=int(candle_unit_minutes),
            candle_count=int(candle_count),
            seconds=int(seconds),
            interval_sec=float(interval_sec),
            chunk_size=int(chunk_size),
            max_markets=max_markets,
        )
    except (TypeError, ValueError) as e:
        logger.warning("ladder_router.longhold_candidates L1072: %s", e)
        raise HTTPException(status_code=500, detail={"error": "LONGHOLD_CANDIDATES_FAILED", "detail": str(e), "strategy": strategy})


@router.get(
    "/longhold/snapshot",
    summary="Get LongHold positions snapshot",
    responses={
        200: {"description": "Current LongHold positions with PnL"},
    },
)
def longhold_snapshot(
    request: Request,
    market: Optional[str] = Query(None, description="Filter by market code"),
    include_disabled: bool = Query(True, description="Include disabled configurations"),
) -> Dict[str, Any]:
    """
    Get a snapshot of current LongHold positions with profit/loss data.
    """
    mgr = _get_mgr(request)
    try:
        return mgr.longhold_snapshot(market=market, include_disabled=include_disabled)
    except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as e:
        logger.warning("ladder_router.longhold_snapshot L1094: %s", e)
        raise HTTPException(status_code=500, detail={"error": "LONGHOLD_SNAPSHOT_FAILED", "detail": str(e), "market": market})


@router.post(
    "/longhold/remove",
    summary="Remove a LongHold configuration",
    responses={
        200: {"description": "LongHold configuration removed"},
    },
)
def longhold_remove(
    request: Request,
    market: str = Query(..., description="Market code to remove"),
) -> Dict[str, Any]:
    """
    Remove the LongHold configuration for a market.
    """
    mgr = _get_mgr(request)
    try:
        return mgr.remove_longhold_config(market)
    except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as e:
        logger.warning("ladder_router.longhold_remove L1115: %s", e)
        raise HTTPException(status_code=500, detail={"error": "LONGHOLD_REMOVE_FAILED", "detail": str(e), "market": market})


@router.post(
    "/longhold/poll",
    summary="Poll LongHold alerts",
    responses={
        200: {"description": "Alert check results"},
    },
)
def longhold_poll(
    request: Request,
    market: Optional[str] = Query(None, description="Filter by market code"),
) -> Dict[str, Any]:
    """
    Poll and trigger alerts for LongHold positions that hit target profit.
    """
    mgr = _get_mgr(request)
    try:
        return mgr.poll_longhold_alerts(market=market)
    except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as e:
        logger.warning("ladder_router.longhold_poll L1136: %s", e)
        raise HTTPException(status_code=500, detail={"error": "LONGHOLD_POLL_FAILED", "detail": str(e), "market": market})

@router.post(
    "/longhold/sync",
    summary="Sync LongHold positions",
    responses={
        200: {"description": "Positions synchronized with exchange"},
    },
)
def longhold_sync(request: Request) -> Dict[str, Any]:
    """
    Trigger system reconcile to update positions for LongHold markets.

    - Syncs position data from exchange
    - Updates internal tracking
    """
    system = request.app.state.system
    system.reconcile(reason="longhold_manual_sync")
    return {"ok": True}


@router.post("/grid/sync", summary="ICAG V3: 그리드 동기화")
def grid_sync(
    request: Request,
    market: str = Query(..., description="Market code (e.g., BTCUSDT)"),
) -> Dict[str, Any]:
    grid_v3 = _get_grid_v3(request)
    try:
        return grid_v3.poll_and_sync(market.upper())
    except (AttributeError, TypeError, ValueError) as e:
        logger.warning("ladder_router.grid_sync L1166: %s", e)
        raise HTTPException(status_code=500, detail={"error": "GRID_SYNC_FAILED", "detail": str(e)})


@router.get("/grid/state", summary="ICAG V3: 그리드 상태 조회")
def grid_state(
    request: Request,
    market: str = Query(..., description="Market code"),
) -> Dict[str, Any]:
    grid_v3 = _get_grid_v3(request)
    mgr = _get_mgr(request)
    
    market = market.upper()
    diag = grid_v3.get_diagnostics(market)
    current_price = mgr.get_current_price(market) or 0
    cfg = mgr.get_config(market)
    
    # Build steps from ICAG targets
    state = grid_v3._get_state(market)
    order_usdt = float(cfg.get("order_usdt") or 10)
    
    # Get active orders from registry
    reg = mgr._read_order_registry()
    market_reg = reg.get(market, {})
    
    steps = []
    for uuid_, meta in market_reg.items():
        if not isinstance(meta, dict):
            continue
        status = str(meta.get("status") or "active").lower()
        if status == "deleted":
            continue
        side = str(meta.get("side") or "").lower()
        price = float(meta.get("price") or 0)
        qty = float(meta.get("qty") or meta.get("volume") or 0)
        if price <= 0:
            continue
        steps.append({
            "price": price,
            "side": "buy" if side in ("buy", "bid") else "sell",
            "status": status,
            "amount": round(price * qty) if qty > 0 else int(order_usdt),
            "uuid": uuid_,
            "filled": status == "filled",
        })
    
    steps.sort(key=lambda s: s["price"], reverse=True)
    
    return {
        "ok": True,
        "market": market,
        "engine": "icag_v3",
        "current_price": current_price,
        "config": cfg,
        "icag": diag,
        "steps": steps,
    }





@router.post(
    "/backfill",
    summary="과거 체결 주문 소급 기록",
    responses={200: {"description": "Backfill result"}},
)
def backfill_filled_orders(
    request: Request,
    since: str = Query("2026-02-08T00:01:00+09:00", description="소급 시작 시각 (ISO 8601)"),
) -> Dict[str, Any]:
    from datetime import datetime
    mgr = _get_mgr(request)
    try:
        dt = datetime.fromisoformat(since)
        since_ts = dt.timestamp()
    except (ValueError, TypeError):
        logger.warning("ladder_router.backfill_filled_orders L1242 except", exc_info=True)
        since_ts = 0.0
    result = mgr.backfill_filled_orders(since_ts=since_ts)
    return {"ok": True, **result}


# --- Auto Tuner ---
@router.get("/tune/status", summary="Auto-tuner 상태 조회")
def tune_status(request: Request) -> Dict[str, Any]:
    """현재 auto-tuner 상태 (각 마켓별 마지막 튜닝 결과)"""
    from app.manager.ladder_auto_tuner import LadderAutoTuner
    mgr = _get_mgr(request)
    tuner = LadderAutoTuner(mgr, system=request.app.state.system)
    return {"ok": True, "history": tuner.get_recent_history(limit=20)}

@router.post("/tune/run", summary="수동 Auto-tune 실행")
def tune_run(
    request: Request,
    market: str = Query(None, description="특정 마켓 (미지정시 전체)"),
    dry_run: bool = Query(False, description="True면 적용 안하고 결과만 반환"),
) -> Dict[str, Any]:
    from app.manager.ladder_auto_tuner import LadderAutoTuner
    mgr = _get_mgr(request)
    tuner = LadderAutoTuner(mgr, system=request.app.state.system)
    if market:
        result = tuner.tune(market.strip().upper(), dry_run=dry_run)
        return {"ok": True, "result": result.__dict__ if hasattr(result, '__dict__') else result}
    else:
        results = tuner.tune_all(dry_run=dry_run)
        return {"ok": True, "results": {k: v.__dict__ if hasattr(v, '__dict__') else v for k, v in results.items()}}

@router.get("/tune/history", summary="튜닝 이력 조회")
def tune_history(
    request: Request,
    market: str = Query(None, description="특정 마켓 필터"),
    limit: int = Query(50, ge=1, le=200),
) -> Dict[str, Any]:
    from app.manager.ladder_auto_tuner import LadderAutoTuner
    mgr = _get_mgr(request)
    tuner = LadderAutoTuner(mgr, system=request.app.state.system)
    history = tuner.get_recent_history(limit=limit, market=market)
    return {"ok": True, "items": history}


# ------------------------------------------------------------
# Circuit Breaker (delegates to LadderGridV2 state)
# ------------------------------------------------------------
def _get_grid_v2(request: Request):
    system = request.app.state.system
    v2 = getattr(system, "_ladder_grid_v2_cb", None)
    if v2 is not None:
        return v2
    from app.manager.ladder_grid_v2 import LadderGridV2
    mgr = _get_mgr(request)
    v2 = LadderGridV2(mgr)
    system._ladder_grid_v2_cb = v2
    return v2


@router.get("/grid/circuit-breaker", summary="Circuit Breaker 상태 조회")
def circuit_breaker_status(request: Request) -> Dict[str, Any]:
    v2 = _get_grid_v2(request)
    return v2.get_circuit_breaker_status()


@router.post("/grid/circuit-breaker/toggle", summary="Circuit Breaker ON/OFF")
def circuit_breaker_toggle(
    request: Request,
    enabled: bool = Query(..., description="Enable or disable circuit breaker"),
) -> Dict[str, Any]:
    v2 = _get_grid_v2(request)
    v2.set_circuit_breaker_enabled(enabled)
    return {"ok": True, "enabled": enabled}


@router.post("/grid/circuit-breaker/threshold", summary="Circuit Breaker 임계값 설정")
def circuit_breaker_threshold(
    request: Request,
    threshold: float = Query(..., ge=1.0, description="Threshold value"),
) -> Dict[str, Any]:
    v2 = _get_grid_v2(request)
    v2.set_circuit_breaker_threshold(threshold)
    return {"ok": True, "threshold": threshold}

