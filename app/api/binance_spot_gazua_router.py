# ============================================================
# Binance 현물(spot) FOCUS Strategy API Router (USDT long_only)
# ------------------------------------------------------------
# BinanceSpotGazuaManager 제어용 REST 엔드포인트. upbit_gazua_router 미러(거래소만 Binance 현물).
# ============================================================
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Query, Request

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/strategy/binance_spot_gazua", tags=["BINANCE_SPOT_GAZUA"])


def _get_um(request: Request):
    """Get BinanceSpotGazuaManager from system (없으면 생성)."""
    system = request.app.state.system
    um = getattr(system, "binance_spot_gazua_manager", None)
    if um is None:
        from app.manager.binance_spot_gazua_manager import BinanceSpotGazuaManager
        um = BinanceSpotGazuaManager(system=system)
        system.binance_spot_gazua_manager = um
    return um


# ── Status / Config ─────────────────────────────────────────
@router.get("/status")
def binance_spot_focus_status(request: Request):
    return {"ok": True, **_get_um(request).get_status()}


@router.get("/config")
def binance_spot_focus_config_get(request: Request):
    return {"ok": True, "config": _get_um(request).get_status()["config"]}


@router.get("/config/defaults")
def binance_spot_focus_config_defaults():
    """코드 기본값(SpotGazuaConfig dataclass) — v3 대시보드 ↺ 리셋 소스. focus 미러."""
    from dataclasses import asdict
    from app.manager.spot_gazua_manager import SpotGazuaConfig
    return {"ok": True, "defaults": asdict(SpotGazuaConfig())}


@router.post("/config")
def binance_spot_focus_config_set(
    request: Request,
    paper: Optional[bool] = Query(None),
    budget: Optional[float] = Query(None, ge=0),
    max_positions: Optional[int] = Query(None, ge=1, le=20),
    max_daily_plans: Optional[int] = Query(None, ge=1, le=999),
    risk_pct: Optional[float] = Query(None, ge=0, le=100),
    conv_sizing_enabled: Optional[bool] = Query(None, description="점수(confidence) 비례 사이징. OFF=균등 1/N."),
    conv_size_floor: Optional[float] = Query(None, ge=0, le=1, description="통과 하한 신호가 쓰는 슬롯 비중(0~1). 1=가중 OFF."),
    min_conf: Optional[float] = Query(None, ge=0, le=1),
    entry_conf_threshold: Optional[float] = Query(None, ge=0, le=1),
    primary_tf: Optional[str] = Query(None),
    top_n: Optional[int] = Query(None, ge=1, le=50),
    scan_interval_sec: Optional[float] = Query(None, ge=1),
    scan_exclude: Optional[str] = Query(None, description="스캔 제외 마켓 (쉼표 구분, 예: BTCUSDT)"),
    block_warning_coins: Optional[bool] = Query(None, description="거래소 투자유의 종목 진입 차단(Binance 현물=해당 없음)."),
    block_caution_coins: Optional[bool] = Query(None, description="거래소 주의환기 종목 진입 차단(Binance 현물=해당 없음)."),
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
    longhold_enabled: Optional[bool] = Query(None, description="SL→존버 전환(§4.2). 기본 OFF."),
    longhold_release_pct: Optional[float] = Query(None, ge=0, description="존버 해제 임계 %. 0=ATR 동적."),
    longhold_max_hold_hours: Optional[float] = Query(None, ge=0, description="존버 최대 보유시간. 0=무제한."),
    headroom_gate_pct: Optional[float] = Query(None, ge=0, description="머리 위 저항 최소 여유 %(§②). 0=OFF, 천장 추격 차단."),
    atr_sl_floor_mult: Optional[float] = Query(None, ge=0, description="SL 거리 최소=mult×ATR(§②). 0=OFF, 잔챙이 즉사 방지."),
    overext_range_pos_pct: Optional[float] = Query(None, ge=0, le=1, description="끝물 차단: 24H 범위 상단 비율↑(예 0.85). 0=OFF, ADX 면제 없음."),
    overext_min_move_pct: Optional[float] = Query(None, ge=0, description="끝물 판정 최소 24H 변동 |%|."),
    blowoff_move_pct: Optional[float] = Query(None, ge=0, description="파라볼릭 차단: 24H |변동|≥%(예 30)+추격. 0=OFF."),
    guard_score_mode_enabled: Optional[bool] = Query(None, description="guard_score(ADX+추세conf) 계산·표시(G1)."),
    guard_score_threshold: Optional[float] = Query(None, description="진입 최소 guard_score. 0=게이트 OFF(표시만)."),
    guard_score_total_cap: Optional[float] = Query(None, ge=0, description="guard_score ±cap 클램프(80+ 억제). 0=무제한."),
    multi_be_lock_enabled: Optional[bool] = Query(None, description="peak 단계별 SL 위로 잠금(이익 보호 ratchet)."),
    multi_be_lock_stage1_pct: Optional[float] = Query(None, ge=0, description="1단계 peak%% → SL=BE+cushion."),
    multi_be_lock_stage2_pct: Optional[float] = Query(None, ge=0, description="2단계 peak%% → SL=entry+0.3%%."),
    multi_be_lock_stage3_pct: Optional[float] = Query(None, ge=0, description="3단계 peak%% → SL=entry+1.0%%."),
    multi_be_lock_stage4_pct: Optional[float] = Query(None, ge=0, description="4단계 peak%% → SL=entry+2.0%%."),
    multi_be_lock_fee_cushion_pct: Optional[float] = Query(None, ge=0, description="BE 잠금 수수료 쿠션 %%."),
    multi_be_lock_atr_adaptive: Optional[bool] = Query(None, description="be_lock ATR 적응 — 메이저(저변동) 노이즈 BE컷 방지(Binance 회전매 fix). 노이즈 위에서만 잠금 시작."),
    multi_be_lock_atr_mult: Optional[float] = Query(None, ge=0, description="arming floor = ATR%% × 이값."),
    be_stall_enabled: Optional[bool] = Query(None, description="be_stall(peak 정체+모멘텀 꺾임 익절 컷)."),
    be_stall_sec: Optional[float] = Query(None, ge=0, description="peak 정체 최소 초(컷 후보)."),
    be_stall_max_since_peak_sec: Optional[float] = Query(None, ge=0, description="stale 컷오프 초(묵은 peak 미발동)."),
    be_stall_neutral_exit: Optional[bool] = Query(None, description="중립 모멘텀도 시간컷? 기본 False(보수)."),
    be_stall_rsi_strong: Optional[float] = Query(None, ge=0, le=100, description="RSI 우리편 기준."),
    be_stall_rsi_weak: Optional[float] = Query(None, ge=0, le=100, description="RSI 반대편 기준."),
    fee_rate_pct: Optional[float] = Query(None, ge=0, le=5, description="한쪽 수수료율 %(Binance 현물 taker≈0.1). 왕복=×2. net PnL 반영."),
    manual_manage_enabled: Optional[bool] = Query(None, description="수동(퀵트레이드) 매수 포지션을 봇이 SL/TP 자동 관리? OFF=관망(청산 버튼으로 사람 수확)."),
    contrarian_enabled: Optional[bool] = Query(None, description="역행(CONTRARIAN) 2번째 진입원 ON/OFF. 기본 OFF. 상승추세엔 OFF, 중립/하락만."),
    contrarian_max_positions: Optional[int] = Query(None, ge=0, le=10, description="역행 별도 슬롯(FOCUS 슬롯과 분리)."),
    contrarian_coin_up_th: Optional[float] = Query(None, ge=0, description="역행 진입 자격: 코인 24h move − BTC move ≥ 이 %%(상대강도)."),
    contrarian_coin_up_cap: Optional[float] = Query(None, ge=0, description="파라볼릭 차단: 코인 24h |move| 이 %%↑면 제외(펌프 함정). 0=OFF."),
    contrarian_regime_gate: Optional[bool] = Query(None, description="True=상승추세(BTC UP)엔 진입 안 함(중립/하락만). False=상시."),
    contrarian_budget: Optional[float] = Query(None, ge=0, description="역행 예산. 0=equity의 contrarian_budget_pct%% / >0=고정 금액."),
    contrarian_budget_pct: Optional[float] = Query(None, ge=0, le=100, description="contrarian_budget=0 일 때 equity 대비 비율 %%."),
    contrarian_tp_pct: Optional[float] = Query(None, ge=0, description="역행 TP1(부분익절) 진입가 +%%."),
    contrarian_tp2_pct: Optional[float] = Query(None, ge=0, description="역행 TP2(전량) 진입가 +%%."),
    contrarian_sl_pct: Optional[float] = Query(None, ge=0, description="역행 SL 진입가 -%%."),
    gap_check_enabled: Optional[bool] = Query(None, description="갭 체크(선물 복사) — 머리 위 N봉 고가까지 거리<필요갭이면 차단(천장 밑 진입 금지)."),
    gap_check_min_pct: Optional[float] = Query(None, ge=0, description="최소 필요 갭 %%."),
    micro_1m_check_enabled: Optional[bool] = Query(None, description="1M 타이밍(선물 복사) — 역봉/거래량소진/RSI과열이면 진입 보류."),
    momentum_reversal_enabled: Optional[bool] = Query(None, description="직전 5M 강한 역행 차단(선물 복사) — 떨어지는 칼 진입 금지."),
    momentum_reversal_strong_atr: Optional[float] = Query(None, ge=0, description="강한 역행 임계 (×5M ATR)."),
    raw_body_enabled: Optional[bool] = Query(None, description="raw_body(선물 복사) — 직전 5M N봉 net 에너지가 진입 반대면 차단."),
    momentum_deriv_enabled: Optional[bool] = Query(None, description="momentum_deriv(선물 복사) — 5M RSI/MACD 변화율이 진입 반대 가속이면 차단(RSI+MACD 둘 다)."),
    mtf_align_enabled: Optional[bool] = Query(None, description="MTF 최종 차단(선물 복사) — 상위/단기 TF(240/30/15) 구조가 명확히 반대면 진입 차단."),
    entry_expectation_enabled: Optional[bool] = Query(None, description="진입 기대치(선물 공유 유틸) — reward 부족 or risk 과대면 차단."),
    entry_expectation_min_reward_pct: Optional[float] = Query(None, ge=0, description="reward < 이 %%면 차단."),
    entry_expectation_max_risk_pct: Optional[float] = Query(None, ge=0, description="risk > 이 %%면 차단."),
    microtiming_5m_enabled: Optional[bool] = Query(None, description="microtiming_5m(선물 복사) — 5M RSI/MACD/BB 변곡 2/3 미만이면 이번 tick 보류(defer)."),
    microtiming_5m_min_score: Optional[int] = Query(None, ge=0, le=3, description="통과 최소 변곡 점수(0~3)."),
):
    """명시적으로 보낸 필드만 갱신 (FOCUS 패턴)."""
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
    # ★ generic: 위에 명시 안 된 672 미러 필드도 query param 으로 수용(update_config 타입강제+hasattr 필터).
    for _k, _v in request.query_params.items():
        if _k not in cfg:
            cfg[_k] = _v
    um.update_config(cfg)
    logger.info("[BINANCE_SPOT_FOCUS_API] config set: %d fields", len(cfg))
    return {"ok": True, "config": um.get_status()["config"]}


# ── Enable / Disable ────────────────────────────────────────
@router.post("/enable")
def binance_spot_focus_enable(
    request: Request,
    paper: Optional[bool] = Query(None, description="paper 모드. 생략 시 현재값 유지(기본 True)."),
    budget: Optional[float] = Query(None, ge=0, description="예산 USDT (0=가용잔고 자동)."),
):
    um = _get_um(request)
    cfg: Dict[str, Any] = {"enabled": True}
    if paper is not None:
        cfg["paper"] = paper
    if budget is not None:
        cfg["budget"] = budget
    um.update_config(cfg)
    logger.info("[BINANCE_SPOT_FOCUS_API] Enabled: %s", {k: v for k, v in cfg.items() if k != "enabled"} or "none")
    return {"ok": True, "enabled": True, **um.get_status()}


@router.post("/disable")
def binance_spot_focus_disable(request: Request):
    um = _get_um(request)
    um.update_config({"enabled": False})
    logger.info("[BINANCE_SPOT_FOCUS_API] Disabled")
    return {"ok": True, "enabled": False}


# ── Scan preview ────────────────────────────────────────────
@router.get("/scan")
def binance_spot_focus_scan(request: Request):
    """3중 확인 스캔 미리보기 (진입은 안 함)."""
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
        logger.warning("[BINANCE_SPOT_FOCUS_API] scan error: %s", exc)
        return {"ok": False, "error": str(exc)}


@router.get("/scan-candidates")
def binance_spot_focus_scan_candidates(request: Request):
    """예비 후보 현황 — 거래대금 상위를 GreenPen 진단(차단 사유 포함). 진입 안 함."""
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
        return {"ok": True, "candidates": rows, "thresholds": {
            "guard_score_threshold": um.config.guard_score_threshold,
            "entry_conf_threshold": um.config.entry_conf_threshold,
            "min_conf": um.config.min_conf,
        }}
    except Exception as exc:
        logger.warning("[BINANCE_SPOT_FOCUS_API] scan-candidates error: %s", exc)
        return {"ok": False, "error": str(exc)}


@router.get("/scan-list")
def binance_spot_focus_scan_list(request: Request):
    """거래대금 상위 후보(Source1) 미리보기."""
    um = _get_um(request)
    try:
        from app.manager.spot_focus_coin_selector import _source1_spot_volume
        markets = _source1_spot_volume(um.client, top_n=um.config.top_n, exclude=um.config.scan_exclude)
        return {"ok": True, "markets": markets}
    except Exception as exc:
        logger.warning("[BINANCE_SPOT_FOCUS_API] scan-list error: %s", exc)
        return {"ok": False, "error": str(exc)}


# ── 공개 호가창 / 점수 궤적 ──────────────────────────────────
@router.get("/score-timeline")
def binance_spot_focus_score_timeline(request: Request, market: str = Query(..., description="마켓 (예: WLDUSDT)"),
                                    count: int = Query(60, ge=10, le=120)):
    """과거 시점별 guard_score+conf 궤적 (점수↔차트 정합 검증)."""
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
        logger.warning("[BINANCE_SPOT_FOCUS_API] score-timeline %s error: %s", market, exc)
        return {"ok": False, "error": str(exc)}


@router.get("/orderbook")
def binance_spot_focus_orderbook(request: Request, market: str = Query(..., description="마켓 (예: BTCUSDT)"),
                               depth: int = Query(15, ge=1, le=30)):
    """Binance 현물 공개 호가창 프록시 (서버 경유)."""
    um = _get_um(request)
    try:
        return {"ok": True, **um.client.get_orderbook(market, depth=depth)}
    except Exception as exc:
        logger.warning("[BINANCE_SPOT_FOCUS_API] orderbook %s error: %s", market, exc)
        return {"ok": False, "error": str(exc)}


# ── Trade Journal ───────────────────────────────────────────
@router.get("/journal")
def binance_spot_focus_journal(request: Request, limit: int = Query(100, ge=1, le=2000)):
    """거래기록(최신순) + 집계(누적/오늘 PnL, 승률, 일별)."""
    um = _get_um(request)
    try:
        return {"ok": True, "rows": um.read_journal(limit=limit), "summary": um.journal_summary()}
    except Exception as exc:
        logger.warning("[BINANCE_SPOT_FOCUS_API] journal error: %s", exc)
        return {"ok": False, "error": str(exc)}


@router.post("/journal/delete")
def binance_spot_focus_journal_delete(request: Request, ts: float = Query(..., description="삭제할 저널 기록의 ts (행 고유값)")):
    """저널 기록 1건 삭제 (ts 매칭). 기록만 삭제 — 거래·포지션 무관."""
    um = _get_um(request)
    try:
        return um.delete_journal(ts)
    except Exception as exc:
        logger.warning("[BINANCE_SPOT_FOCUS_API] journal delete error: %s", exc)
        return {"ok": False, "error": str(exc)}


# ── 퀵 트레이드 (수동 즉시 시장가, LIVE 전용) ─────────────────
@router.post("/order")
def binance_spot_focus_order(
    request: Request,
    market: str = Query(..., description="마켓 (예: BTCUSDT 또는 BTC)"),
    side: str = Query(..., description="buy(매수) 또는 sell(매도)"),
    krw: float = Query(0.0, ge=0, description="매수 금액(USDT) — 원 모드"),
    qty: float = Query(0.0, ge=0, description="매도 수량(0=실보유 전량) — 원 모드"),
    pct: float = Query(0.0, ge=0, le=100, description="비율 % — 매수=가용USDT×%, 매도=보유×%. >0이면 krw/qty 무시(% 모드)."),
):
    """대시보드 퀵트레이드 — 즉시 시장가 주문. paper 모드 차단."""
    um = _get_um(request)
    try:
        res = um.quick_order(market, side, krw=krw, qty=qty, pct=pct)
        logger.info("[BINANCE_SPOT_FOCUS_API] quick_order %s %s -> %s", market, side, res.get("ok"))
        return res
    except Exception as exc:
        logger.warning("[BINANCE_SPOT_FOCUS_API] order %s %s error: %s", market, side, exc)
        return {"ok": False, "error": str(exc)}


@router.post("/force-close")
def binance_spot_focus_force_close(request: Request, market: str = Query(..., description="강제청산할 봇 관리 포지션 마켓 (예: BTCUSDT)")):
    """봇 관리 포지션 1개 강제청산 (사람 수확). paper/live 공통."""
    um = _get_um(request)
    try:
        res = um.force_close(market)
        logger.info("[BINANCE_SPOT_FOCUS_API] force_close %s -> %s", market, res.get("ok"))
        return res
    except Exception as exc:
        logger.warning("[BINANCE_SPOT_FOCUS_API] force_close %s error: %s", market, exc)
        return {"ok": False, "error": str(exc)}


# ── 📊 near-miss 사후판정 (과차단 탐지 · long-only 방패) ──────────
@router.get("/near-miss")
def gazua_near_miss(request: Request):
    """guard_score 통과 후 막판 게이트에 막힌 매수의 '차단가 대비 수익률' 사후판정.
    막은 뒤 ↑(아쉬운 차단)=과차단 신호 / 그대로·↓(좋은 차단). kline 사용 →
    get_near_miss_enriched 의 25s 응답캐시로 묶음(운영자 거래소 Tick 걱정 반영, 서버간 Tick 무관)."""
    um = _get_um(request)
    try:
        return {"ok": True, "near_miss": um.get_near_miss_enriched()}
    except Exception as exc:
        logger.warning("[GAZUA_API] near-miss error: %s", exc)
        return {"ok": False, "error": str(exc)}


# ── 🕊️ 대사면(amnesty) — 유치장 코인 입양 ────────────────────
@router.get("/orphans")
def binance_spot_focus_orphans(request: Request):
    """거래소 보유 중 봇 밖에 갇힌 코인(사면 후보) 조회. 정보만."""
    um = _get_um(request)
    try:
        return {"ok": True, "orphans": um.list_orphans()}
    except Exception as exc:
        logger.warning("[BINANCE_SPOT_FOCUS_API] orphans error: %s", exc)
        return {"ok": False, "error": str(exc)}


@router.post("/adopt")
def binance_spot_focus_adopt(request: Request, market: str = Query(..., description="사면할 코인 마켓 (예: WLDUSDT)")):
    """사면 — 운영자가 고른 코인 하나만 봇 관리로 입양(자동 입양 X)."""
    um = _get_um(request)
    try:
        res = um.adopt_orphan(market)
        logger.info("[BINANCE_SPOT_FOCUS_API] 🕊️ adopt %s -> %s", market, res.get("ok"))
        return res
    except Exception as exc:
        logger.warning("[BINANCE_SPOT_FOCUS_API] adopt %s error: %s", market, exc)
        return {"ok": False, "error": str(exc)}
