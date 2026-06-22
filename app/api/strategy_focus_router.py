# ============================================================
# FOCUS Strategy API Router
# ------------------------------------------------------------
# REST endpoints for controlling the FOCUS strategy manager.
# ============================================================
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Query, Request

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/strategy/focus", tags=["FOCUS"])


def _get_fm(request: Request):
    """Get FocusManager from system."""
    system = request.app.state.system
    fm = getattr(system, "focus_manager", None)
    if fm is None:
        from app.manager.focus_manager import FocusManager
        fm = FocusManager(system=system)
        system.focus_manager = fm
    return fm


def _bot_opinion(signal: str, trend: str, gs_total, gs_threshold,
                 block_reason: str, pa_pattern: str) -> dict | None:
    """[2026-05-28 부모] 봇과 표면 시그널이 어긋날 때 경고 배지 데이터.

    어제 XLM LONG 사례: 점수 100점인데 봇 내부는 BB 129% 과열로 SHORT flip 시도.
    수동 진입 시 `_is_manual` 충돌 차단으로 막혔지만 부모님이 *왜* 막혔는지
    Dashboard 에서 보이지 않음. 이 함수가 의도/내부의견 충돌을 미리 노출한다.

    return None 이면 배지 미표시. dict 면:
        level: "warn"(빨강) | "info"(노랑)
        text: 한 줄 한국어
    """
    br = (block_reason or "")
    pa = (pa_pattern or "")
    try:
        gst = float(gs_total) if gs_total is not None else None
        gth = float(gs_threshold) if gs_threshold is not None else 65.0
    except Exception:
        gst, gth = None, 65.0

    # 우선순위 1: BB 극단 → 반대방향 flip 권장 (가장 강한 충돌)
    if signal == "BUY":
        if "FLIP BUY" in br or "overbought" in br:
            return {"level": "warn", "text": "⚠ BB 과열 — SHORT 권장"}
        if "BOS_BEARISH" in br or "BOS_BEARISH" in pa:
            return {"level": "warn", "text": "⚠ 하락 BOS + BB 과매도 — LONG 위험"}
        if trend == "DOWNTREND":
            return {"level": "warn", "text": "⚠ H4 하락 추세 — LONG 부적합"}
    elif signal == "SELL":
        if "FLIP SELL" in br or "oversold" in br:
            return {"level": "warn", "text": "⚠ BB 극단 — LONG 권장"}
        if "BOS_BULLISH" in br or "BOS_BULLISH" in pa:
            return {"level": "warn", "text": "⚠ 상승 BOS — SHORT 위험"}
        if trend == "UPTREND":
            return {"level": "warn", "text": "⚠ H4 상승 추세 — SHORT 부적합"}

    # 우선순위 2: 30M 방향 충돌
    if "30M dir conflict" in br or "30M UPTREND" in br or "30M DOWNTREND" in br:
        return {"level": "warn", "text": "⚠ 30M 방향 충돌"}

    # 우선순위 3: 점수 음수 (강한 약함)
    if gst is not None and gst < 0 and signal in ("BUY", "SELL"):
        return {"level": "info", "text": f"🔸 점수 음수({int(gst)}) — 진입 매우 약함"}

    # 우선순위 4: 점수 threshold 미달이지만 양수 (근접)
    if gst is not None and 0 <= gst < gth and signal in ("BUY", "SELL"):
        if gst >= gth * 0.7:  # threshold 70%+ 근접
            return {"level": "info", "text": f"🔸 점수 근접({int(gst)}/{int(gth)})"}

    return None


# ── Status ──────────────────────────────────────────────────

# /status 응답 캐시 — get_status() 가 매 호출 포지션별 현재가 fetch + _get_b12_breadth_vote(저널
# 캐시 풀스캔, 결과 TTL 없음) + today_pnl 등 무겁다. 대시보드가 다탭으로 1~2초마다 폴링하면 동시부하 +
# 브라우저 호스트당 6연결 큐에 막혀 4.5s timeout → canceled → FOCUS 패널 통째 "상태 로딩 중" 멈춤.
# get_status(self) 는 요청 무관(fleet-global)이라 짧은 TTL 로 공유 재사용 → 다탭이 1회 계산을 나눠 씀.
# [2026-06-19 부모 "모든 서버에서 focus 통째로 로딩"]. peer-cache(cd7a2b9)와 동일 패턴.
_STATUS_RESP_BOX: dict = {"ts": 0.0, "data": None}
_STATUS_RESP_TTL = 3.0   # 초 — PnL/포지션 표시라 짧게(3s staleness 무시 가능). 멈춤은 동시부하라 dedup 만으로 해소.


@router.get("/status")
def focus_status(request: Request):
    """Current FOCUS state, position, PnL, daily discipline.
    ★ 응답을 _STATUS_RESP_TTL 초 캐시 → 다탭/다폴링이 무거운 get_status() 1회를 재사용(통째 멈춤 방지)."""
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
    logger.info("[FOCUS_API] Enabled: overrides=%s", {k: v for k, v in cfg.items() if k != "enabled"} or "none")
    return {"ok": True, "enabled": True, **fm.get_status()}


@router.post("/disable")
def focus_disable(
    request: Request,
    close_position: bool = Query(False, description="Close open position before disabling"),
):
    """Disable FOCUS strategy."""
    fm = _get_fm(request)

    # ★ H8 FIX: multi-position 대응 — position 또는 positions 있으면 청산
    if close_position and (fm.position or fm.positions):
        try:
            fm._execute_exit("manual_disable", is_sl=False)
        except Exception as exc:
            logger.warning("[FOCUS_API] Position close on disable failed: %s", exc)

    fm.update_config({"enabled": False})
    logger.info("[FOCUS_API] Disabled (close_position=%s)", close_position)
    return {"ok": True, "enabled": False}


@router.post("/close-all")
def focus_close_all(request: Request):
    """Close ALL FOCUS positions on Bybit and clear local state.
    ★ Bybit 실제 포지션을 조회하여 전부 청산 (유령 포지션 방지)."""
    fm = _get_fm(request)
    closed = []
    errors = []

    # ★ 재진입 쿨다운 갱신 — close-all 후 Scanner 즉시 재진입 방지
    import time as _time
    fm._last_exit_ts = _time.time()
    if fm.positions:
        fm._last_exit_market = fm.positions[0].market
        fm._last_exit_direction = fm.positions[0].direction

    # 1. 로컬 positions 리스트의 포지션 청산 — _close_position 사용 (저널 기록 포함)
    for pos in list(fm.positions):
        try:
            ok = fm._close_position(pos, reason="manual_close_all")
            if ok:
                closed.append({"market": pos.market, "direction": pos.direction, "qty": pos.qty})
                logger.info("[FOCUS_API] Closed %s %s qty=%.4f", pos.direction, pos.market, pos.qty)
            else:
                errors.append({"market": pos.market, "error": "close_failed"})
        except Exception as exc:
            errors.append({"market": pos.market, "error": str(exc)})
            logger.warning("[FOCUS_API] Close failed %s: %s", pos.market, exc)

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

    # 3. ★ Bybit 실제 열린 포지션 전수 조회 → 유령 포지션 청산
    try:
        import os, time as _t, hashlib, hmac
        from app.core.rate_limiter import bybit_get
        key = os.environ.get('BYBIT_API_KEY', '')
        secret = os.environ.get('BYBIT_API_SECRET', '')
        ts = str(int(_t.time() * 1000))
        recv = '5000'
        params = 'category=linear&settleCoin=USDT'
        sign_str = ts + key + recv + params
        sig = hmac.new(secret.encode(), sign_str.encode(), hashlib.sha256).hexdigest()
        headers = {'X-BAPI-API-KEY': key, 'X-BAPI-TIMESTAMP': ts,
                   'X-BAPI-RECV-WINDOW': recv, 'X-BAPI-SIGN': sig}
        import requests as _req
        r = _req.get('https://api.bybit.com/v5/position/list',
                     params={'category': 'linear', 'settleCoin': 'USDT'},
                     headers=headers, timeout=5)
        bybit_positions = r.json().get('result', {}).get('list', [])
        open_positions = [p for p in bybit_positions if float(p.get('size', 0)) > 0]

        if open_positions:
            logger.warning("[FOCUS_API] Found %d orphan positions on Bybit — closing all", len(open_positions))

        for bp in open_positions:
            sym = bp.get('symbol', '')
            side = bp.get('side', '')
            size = bp.get('size', '0')
            close_side = 'Sell' if side == 'Buy' else 'Buy'
            try:
                fm._get_client().place_order(
                    market=sym, side=close_side, ord_type="market", volume=float(size))
                upl = float(bp.get('unrealisedPnl', 0))
                closed.append({"market": sym, "direction": side, "qty": float(size), "uPnL": round(upl, 2), "orphan": True})
                logger.info("[FOCUS_API] Orphan closed: %s %s qty=%s uPnL=%.2f", sym, side, size, upl)
            except Exception as exc:
                errors.append({"market": sym, "error": str(exc), "orphan": True})
                logger.error("[FOCUS_API] Orphan close FAILED %s: %s", sym, exc)
            _t.sleep(0.3)
    except Exception as exc:
        logger.error("[FOCUS_API] Bybit position scan failed: %s", exc)
        errors.append({"market": "BYBIT_SCAN", "error": str(exc)})

    # 4. ★ Bybit 스캔 실패 시 로컬 상태 유지 (유령 방지)
    from app.manager.focus_manager import FocusState
    bybit_scan_failed = any(e.get("market") == "BYBIT_SCAN" for e in errors)
    if bybit_scan_failed and not closed:
        # Bybit 연결 자체가 실패 → 로컬 상태 유지 (다음 sync에서 정리)
        logger.warning("[FOCUS_API] Bybit scan failed — keeping local state to prevent orphans")
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
        logger.error("[FOCUS_API] close-all completed with %d errors: %s", len(errors), errors)

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

    # 해당 포지션 찾기
    target = None
    for p in fm.positions:
        if p.market.upper() == market.upper():
            target = p
            break

    if not target:
        return {"ok": False, "error": f"{market} not found in positions"}

    try:
        # ★ [2026-04-18] 수익/손실 판정용: close 전에 현재가 캡처
        # 수익 수동 탈출은 "익절" → 4시간 페널티 면제 (사용자 명시 요청)
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
            # ★ 수동 퇴장 페널티 — config 토글 + 수익/손실 판정
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
            # ★ 실패 원인 상세: Bybit에 직접 재시도하며 에러 캡처
            detail = f"Close order failed for {market}"
            try:
                client = fm._get_client()
                side = "Sell" if target.direction == "LONG" else "Buy"
                client.place_order(market=target.market, side=side, ord_type="market", volume=target.qty, reduce_only=True)
                # 여기 도착하면 실은 성공한 거 → 포지션 제거
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
                # ★ 고스트 감지: reduceOnly 관련 에러 또는 포지션 없음 → 로컬에서 자동 제거
                ghost_keywords = ["reduce only", "reduceonly", "position", "110017", "110043", "not enough", "qty not enough"]
                if any(kw in err_str.lower() for kw in ghost_keywords):
                    fm.positions = [p for p in fm.positions if p.market.upper() != market.upper()]
                    fm.position = fm.positions[0] if fm.positions else None
                    if not fm.positions:
                        from app.manager.focus_manager import FocusState
                        fm.state = FocusState.DORMANT
                        fm.selected_market = ""
                    fm._save_config()
                    logger.warning("[FOCUS_API] Ghost detected & removed: %s (%s)", market, err_str)
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
            # ★ [2026-04-18] 수익/손실 판정용: close 전에 현재가 캡처
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
                # ★ 수동 퇴장 페널티 — config 토글 + 수익/손실 판정
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

    # 남은 포지션 정리
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
    """재시작 후 Bybit에 TP/SL 재설정 (증발 복구)."""
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
    """고스트 포지션 제거 — Bybit에서 이미 청산됐지만 로컬에 남아있는 포지션 정리."""
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
        logger.info("[FOCUS_API] Ghost removed: %s (%d positions remaining)", mkt, len(fm.positions))
    return {"ok": removed > 0, "removed": mkt if removed > 0 else None, "remaining": len(fm.positions)}


@router.post("/amnesty")
def focus_amnesty(request: Request):
    """★ 2026-04-23 부모 직접 지시: 대사면 (General Amnesty).

    "법이 미비할 때 저지른 일이니 일단 풀어주자"

    B11 1명 판단 시대의 모든 누적 형벌 기록 해제:
    - _last_exit_* (재진입 차단)
    - _manual_exit_penalties (수동 퇴장 페널티)
    - amnesty_ts 이후 journal 이벤트만 penalty 계산에 사용
      (B12 vote / direction_exhaustion / profit_exit_block 등 모두 영향)

    Journal 기록 자체는 보존 (감사 추적 유지)."""
    fm = _get_fm(request)
    result = fm.execute_amnesty()
    logger.info("[FOCUS_API] ★ AMNESTY granted via API: %s", result)
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
    logger.info("[FOCUS_API] State cleared: %d positions removed from tracking", count)
    return {"ok": True, "cleared": count, "state": "DORMANT"}


# ── Debug: Scanner 후보 진단 ──────────────────────────────
@router.get("/debug/scanner")
def focus_debug_scanner(request: Request):
    """Scanner 후보 + 각 필터 결과 반환 (진단용)."""
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
    """WhaleRadar 현재 상태 — 활성 알림 + 최근 히스토리."""
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
    """Get FOCUS configuration dataclass factory defaults (기본값 리셋 용)."""
    from dataclasses import asdict
    from app.manager.focus_manager import FocusConfig
    return {"ok": True, "defaults": asdict(FocusConfig())}


# ────────────────────────────────────────────────────────────
# ★ [2026-05-19 부모 결정] 자동 config snapshot 시스템 (히스토리 10개)
# 부모님 비전: "10회 설정이 나오면 나중에 수익 극대화된 셋팅이 무엇이었는지 확인"
# 매 POST /config 시 자동 저장 → runtime/config_snapshots/snapshot_YYYYMMDD_HHMMSS.json
# 10개 초과 시 oldest 자동 삭제 (FIFO).
# ────────────────────────────────────────────────────────────
_PNL24H_CACHE = {"t": 0.0, "v": {}}
_PNL24H_TTL = 60.0


def _calc_pnl_24h() -> dict:
    """focus_harpoon_journal.jsonl 의 24h EXIT 통계.

    ★ [2026-06-20] TTL 캐시(60s) — 매 호출 raw 저널 풀파싱(11만줄, 느린 단일코어 서버 ~33s)
    방지. config 저장 스냅샷이 이걸 호출해 저장 POST 가 33s 블록되던 근본(드로다운 리셋 2s 와 대비).
    """
    import time as _t24
    if _PNL24H_CACHE["v"] and (_t24.time() - _PNL24H_CACHE["t"]) < _PNL24H_TTL:
        return _PNL24H_CACHE["v"]
    try:
        from pathlib import Path
        import json as _json
        from datetime import datetime, timedelta
        journal = Path("runtime/focus_harpoon_journal.jsonl")
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
    """config 변경 시 snapshot 저장. 10개 maintain."""
    try:
        from pathlib import Path
        import json as _json
        from datetime import datetime
        import time
        snap_dir = Path("runtime/config_snapshots")
        snap_dir.mkdir(parents=True, exist_ok=True)
        ts = time.time()
        dt = datetime.fromtimestamp(ts)
        fname = f"snapshot_{dt.strftime('%Y%m%d_%H%M%S')}.json"
        fpath = snap_dir / fname
        # patch_diff = 실제 변경된 항목만 (None 제외, list 는 그대로)
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
        # 10개 초과 시 oldest 삭제 (FIFO)
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
    """저장된 snapshot 10개 목록 (timestamp desc).

    Returns: [{filename, ts, dt, patch_diff, pnl_24h}, ...]
    """
    try:
        from pathlib import Path
        import json as _json
        snap_dir = Path("runtime/config_snapshots")
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
    """단일 snapshot 의 전체 config 반환 (복원용)."""
    try:
        from pathlib import Path
        import json as _json
        snap_dir = Path("runtime/config_snapshots")
        # 보안: filename 검증 (snapshot_YYYYMMDD_HHMMSS.json 만 허용)
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
    max_positions: Optional[int] = Query(None, ge=1, le=99, description="최대 동시 포지션 슬롯"),
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
    dynamic_trailing: Optional[bool] = Query(None, description="동적 트레일링 SL ON/OFF"),
    breakeven_trigger_pct: Optional[float] = Query(None, ge=0.1, le=5.0, description="손익분기 잠금 트리거 (%)"),
    trailing_preserve_pct: Optional[float] = Query(None, ge=10, le=90, description="최고수익 보존율-base (%)"),
    trailing_small_profit_preserve_pct: Optional[float] = Query(None, ge=10, le=95, description="소이익 보존율 (<0.5%) (%)"),
    trailing_accel_pct: Optional[float] = Query(None, ge=0, le=30, description="수익 1%당 보존율 가속 (%)"),
    # ── v2: ADX / Conviction ──
    adx_filter_enabled: Optional[bool] = Query(None, description="ADX 필터 ON/OFF"),
    min_adx_entry: Optional[int] = Query(None, ge=10, le=50, description="최소 ADX (진입 기준)"),
    dormant_adx_threshold: Optional[int] = Query(None, ge=5, le=30, description="DORMANT ADX 기준"),
    min_conviction: Optional[float] = Query(None, ge=0, le=100, description="[2026-05-17 100점] 최소 확신 점수 (0~100)"),
    # ── Scanner Multi-Slot ──
    scanner_entry: Optional[bool] = Query(None, description="Scanner multi-slot ON/OFF"),
    scanner_min_adx: Optional[int] = Query(None, ge=15, le=50),
    scanner_min_conviction: Optional[float] = Query(None, ge=0, le=100),
    scanner_max_exposure_pct: Optional[float] = Query(None, ge=10, le=100),
    scanner_m30_primary_conflict_penalty: Optional[float] = Query(None, ge=0.1, le=1.0, description="PRI(H1) vs 30M 추세 충돌 시 conviction 배수 (default 0.7 = -30%, 1.0=OFF, 0.5=옛값)"),
    scanner_m30_direction_conflict_penalty: Optional[float] = Query(None, ge=0.1, le=1.0, description="direction(LONG/SHORT) vs 30M 추세 충돌 시 conviction 배수 (default 0.7 = -30%, 1.0=OFF, 0.5=옛값)"),
    # ── ★ [2026-05-20 Phase 6 재설계] Entry Mode (Score vs Reverse) ──
    entry_mode: Optional[str] = Query(None, regex="^(score|reverse)$", description="진입 모드: score (conviction 100점, 기본) / reverse (봇 신호 + 낮은 conv + 낮은 ADX → 자동 반대 진입, 운영자 1차 룰)"),
    # ── ★★★ [2026-05-28 저녁 부모 결단] 가드 묶음 마스터 토글 ★★★ ──
    # 9개월 누적 가드 정리 — 부모님 분류: 정밀 진입 + 손해 X 이윤 극대화 = 2 종류만.
    # 🟢 green = Phase 6/7 (D1+H4+H1+30M+15M+5M + PA + 5조건 룰) — 적극 진입
    # 🟡 yellow = 옛 깐깐 가드 (BE Stall / Pre-BE / Reverse Drift / Entry Quality Gates 등)
    # both = 둘 다 ON (가장 깐깐, 권장 X)
    # minimal = 핵심만 (TF+PA+SL/HardROE — "죽을 때까지 인내" 모드)
    entry_guard_set: Optional[str] = Query(None, regex="^(green|yellow|both|minimal)$", description="🟢 green (적극, Phase 6/7) / 🟡 yellow (신중, 옛 깐깐 가드) / both / minimal. 한 클릭으로 진입 가드 묶음 통째 전환. default=green."),
    exit_guard_set: Optional[str] = Query(None, regex="^(green|yellow|both|minimal)$", description="🟢 green (charge_exit/tight_trail/exit_5m 등) / 🟡 yellow (BE Stall/Pre-BE/Reverse Drift 등 옛 EXIT 11종) / both / minimal (SL/HardROE 만 — 죽을 때까지 인내). default=green."),
    smart_manual_entry_enabled: Optional[bool] = Query(None, description="[2026-05-29 운영자] Smart Manual Entry (신호 확인 후 진입) ON/OFF. OFF 시 L⏳/S⏳ 버튼 작동 X (즉시 진입 L/S 만)."),
    smart_manual_entry_default_timeout_sec: Optional[float] = Query(None, ge=60, le=86400, description="Smart Manual Entry 대기 시간 (초). UI는 분 단위 입력 → ×60. default 3600 (1시간)."),
    slot_auto_expand_enabled: Optional[bool] = Query(None, description="[2026-05-29 운영자] Slot Auto Expand (강신호 시 임시 +1 슬롯) ON/OFF. 옛 단순 시간 기반 (롤백) → 강신호+묶임+제한적+자본보호 재설계."),
    slot_auto_expand_lock_hours: Optional[float] = Query(None, ge=0.1, le=24, description="모든 슬롯 N시간 묶임 조건 (default 1.0)"),
    slot_auto_expand_min_conviction: Optional[float] = Query(None, ge=50, le=100, description="강신호 최소 conv (default 85)"),
    slot_auto_expand_max_extra: Optional[int] = Query(None, ge=1, le=3, description="최대 추가 슬롯 (default 1, 무한 확장 X)"),
    slot_auto_expand_size_ratio: Optional[float] = Query(None, ge=0.1, le=1.0, description="평균 사이즈의 N% (default 0.5 = 50%, 자본 보호)"),
    market_consensus_exit_enabled: Optional[bool] = Query(None, description="[2026-05-29 운영자] Market Consensus Exit (시장 합의 역방향 손실 청산) ON/OFF. '이윤은 엎치락뒷치락 / 손해는 미끄럼틀' 비대칭 대응."),
    market_consensus_threshold_pct: Optional[float] = Query(None, ge=50, le=100, description="한 방향 신호 비율 임계 (default 70%)"),
    market_consensus_duration_min: Optional[float] = Query(None, ge=1, le=60, description="합의 지속 시간 (default 15분)"),
    market_consensus_min_hold_min: Optional[float] = Query(None, ge=5, le=240, description="포지션 최소 hold (진입 직후 보호, default 20분)"),
    market_consensus_min_pnl_pct: Optional[float] = Query(None, ge=-10, le=0, description="PnL ≤ N%만 청산 (수익 중 역방향 보호, default -0.5%)"),
    reverse_conv_threshold: Optional[float] = Query(None, ge=20, le=70, description="Reverse 모드 conv 임계. 이 값 이하 + ADX 조건 충족 시 반대 진입. default 50 (40~55 권장)"),
    reverse_adx_max: Optional[float] = Query(None, ge=5, le=40, description="Reverse 모드 ADX 임계. 이 값 이하 (= 럭비공 자리) + conv 조건 충족 시 반대 진입. default 20 (15~25 권장)"),
    # ── ★ [2026-05-21 Phase 6 Stage 8 재재설계] 점수 회복 자동 청산 ──
    charge_exit_enabled: Optional[bool] = Query(None, description="점수 회복 자동 청산 ON/OFF. 운영자 룰: '이윤이 나야 청산이고 BE까지 못가더라도 점수가 회복되는 기미 보이면 탈출'. 이윤 중 + conv 회복 시 소액 청산. 기존 가드 (BE/SL) 그대로."),
    charge_exit_min_pnl_pct: Optional[float] = Query(None, ge=-5, le=5, description="점수회복 청산 이윤 조건 (%). 이 이상 pnl일 때만 트리거. default 0 = pnl>0 (이윤 중). 운영자 '이윤이 나야 청산'"),
    charge_exit_conv_delta: Optional[float] = Query(None, ge=1, le=30, description="점수회복 청산 conv 임계. 진입 시 conv 대비 이 이상 증가 시 청산. default 5 (3=민감, 10=보수). 운영자 '점수 회복 기미'"),
    max_same_direction: Optional[int] = Query(None, ge=1, le=15, description="같은 방향 최대 포지션 수"),
    regime_direction_lock_freeze_sec: Optional[float] = Query(None, ge=300, le=86400, description="Regime 변경 후 freeze 시간(초). 30분=1800, 1h=3600, 4h=14400"),
    regime_direction_lock_neutral_block: Optional[bool] = Query(None, description="NEUTRAL regime 시 양방향 차단 (REST). true=쉬기, false=양방향 허용"),
    # ── Coin Loss Cap ──
    coin_loss_cap_enabled: Optional[bool] = Query(None, description="코인별 24h 손실 한도 ON/OFF"),
    coin_loss_cap_amount: Optional[float] = Query(None, ge=5, le=500, description="코인당 최대 손실 ($)"),
    coin_loss_cap_window_hours: Optional[float] = Query(None, ge=1, le=72, description="손실 집계 윈도우 (시간)"),
    # ── Per-Coin Size Cap (★ 2026-05-08 부모님 결정) ──
    per_coin_size_cap_enabled: Optional[bool] = Query(None, description="1코인 사이즈 자본 % 이하 cap ON/OFF"),
    per_coin_size_cap_pct: Optional[float] = Query(None, ge=1, le=100, description="자본 대비 1코인 사이즈 최대 %"),
    # ── Conviction Override Slot (★ 2026-05-10 부모님 결정) ──
    override_slot_enabled: Optional[bool] = Query(None, description="확장 슬롯 ON/OFF (window(h) 이상 묶인 슬롯 만큼 추가 진입)"),
    override_min_conviction: Optional[float] = Query(None, ge=0, le=100, description="[100점] 확장 슬롯 진입 최소 conviction (default 75)"),
    override_locked_slot_min_hours: Optional[float] = Query(None, ge=1, le=720, description="★ 묶인 슬롯 인정 window(h) — 이 시간 이상 보유한 슬롯만 카운트 (default 24)"),
    override_size_cap_pct: Optional[float] = Query(None, ge=1, le=50, description="확장 슬롯 사이즈 cap (자본 %, default 8)"),
    override_max_sl_distance_pct: Optional[float] = Query(None, ge=1, le=50, description="확장 슬롯 max SL 거리 % (default 5)"),
    override_hard_roe_cut_pct: Optional[float] = Query(None, ge=-100, le=0, description="확장 슬롯 Hard ROE 즉시 컷 % (default -10)"),
    # ── Momentum Reversal (Phase 4 의 hard penalty 18) ──
    momentum_reversal_enabled: Optional[bool] = Query(None, description="모멘텀 역행 감점 ON/OFF (진입 직전 5m 1~3봉 역행)"),
    momentum_reversal_strong_atr: Optional[float] = Query(None, ge=0.1, le=5.0, description="강한 역행 ATR 임계 (default 1.0)"),
    momentum_reversal_medium_atr: Optional[float] = Query(None, ge=0.1, le=5.0, description="중간 역행 ATR 임계 (default 0.5)"),
    momentum_reversal_strong_weight: Optional[float] = Query(None, ge=-100, le=0, description="[100점 ×10] 강한 역행 감점 (default -30)"),
    momentum_reversal_medium_weight: Optional[float] = Query(None, ge=-100, le=0, description="[100점 ×10] 중간 역행 감점 (default -20)"),
    momentum_reversal_lookback_bars: Optional[int] = Query(None, ge=1, le=5, description="5m 누적 lookback 봉수 (default 3)"),
    # ── Coin Repeat Brake ──
    coin_repeat_brake_enabled: Optional[bool] = Query(None, description="코인 반복 진입 브레이크 ON/OFF"),
    coin_repeat_free_count: Optional[int] = Query(None, ge=0, le=20, description="무료 진입 횟수 (0 = 첫 진입부터 cooldown 적용)"),
    coin_repeat_cooldown_base: Optional[float] = Query(None, ge=60, le=3600, description="쿨다운 기본 초"),
    # ── ★ BE Stall Exit (2026-05-14 부모 — UI 노출) ──
    be_stall_exit_enabled: Optional[bool] = Query(None, description="BE Stall Exit: BE 후 정체 시 청산 ON/OFF"),
    be_stall_exit_sec: Optional[float] = Query(None, ge=5.0, le=300.0, description="BE Stall Exit: BE 후 정체 시간 초 (default 30)"),
    be_stall_intelligent_enabled: Optional[bool] = Query(None, description="BE Stall 지능형: 모멘텀(MACD/RSI/BB 5m) 연동 — 우리편 HOLD / 반대편 즉시 청산 / 중립 폴백"),
    be_stall_intelligent_rsi_strong: Optional[float] = Query(None, ge=50.0, le=80.0, description="지능형 RSI 강세 임계 (LONG: ≥ 이 값 = 우리편 / 기본 55)"),
    be_stall_intelligent_rsi_weak: Optional[float] = Query(None, ge=20.0, le=50.0, description="지능형 RSI 약세 임계 (LONG: ≤ 이 값 = 반대편 / 기본 45)"),
    # ── ★ Pre-BE Stall Exit (2026-04-23 부모 직접 요청) ──
    pre_be_stall_exit_mode: Optional[str] = Query(None, description="Pre-BE Stall: AUTO (시장따라) / ON / OFF"),
    pre_be_stall_min_profit_pct: Optional[float] = Query(None, ge=0.01, le=2.0, description="Pre-BE Stall: 최소 수익 % (default 0.10)"),
    pre_be_stall_sec: Optional[float] = Query(None, ge=10.0, le=600.0, description="Pre-BE Stall: 정체 시간 초 (default 60)"),
    pre_be_stall_volatility_threshold_pct: Optional[float] = Query(None, ge=0.5, le=10.0, description="Pre-BE Stall AUTO 임계 ATR% (default 2.0, 미만=횡보=ON)"),
    pre_be_stall_max_since_peak_sec: Optional[float] = Query(None, ge=300.0, le=86400.0, description="Pre-BE Stall: peak 후 최대 시간 (default 1800=30분, 넘으면 stale → 미발동)"),
    # ── 🐢 Pre-BE 손실방지선 (2026-06-09 부모 "지금 나우") ──
    pre_be_loss_guard_enabled: Optional[bool] = Query(None, description="🐢 Pre-BE 손실방지선: peak<0.1 헤맴이 entry 손실로 밀리면 작은 컷 (default OFF)"),
    pre_be_loss_guard_peak_max_pct: Optional[float] = Query(None, ge=0.0, le=1.0, description="peak ≤ 이 값 = 헤맴 대상 (default 0.10)"),
    pre_be_loss_guard_trigger_loss_pct: Optional[float] = Query(None, ge=0.1, le=3.0, description="entry 대비 -이 값% 밀리면 컷 (default 0.5, SL 절반)"),
    pre_be_loss_guard_min_hold_sec: Optional[float] = Query(None, ge=0.0, le=3600.0, description="진입 후 최소 보유 초 (default 60)"),
    pre_be_loss_guard_max_age_sec: Optional[float] = Query(None, ge=60.0, le=86400.0, description="stale 보호 — 이 시간 넘으면 미발동 (default 7200=2h)"),
    # ── ★ Reverse Drift Exit (2026-05-16 부모 직접 요청) ──
    reverse_drift_exit_enabled: Optional[bool] = Query(None, description="Reverse Drift Exit: peak에서 역행 시 컷 (pre_be_stall 보완, ATR 무관 발동)"),
    reverse_drift_peak_min_pct: Optional[float] = Query(None, ge=0.01, le=1.0, description="발동 peak 최소 % (default 0.10)"),
    reverse_drift_peak_max_pct: Optional[float] = Query(None, ge=0.05, le=2.0, description="발동 peak 최대 % (default 0.35 = BE_trigger 0.4 미만, 0.05 갭)"),
    reverse_drift_min_since_peak_sec: Optional[float] = Query(None, ge=30.0, le=1800.0, description="peak 후 정체 최소 시간 초 (default 180)"),
    reverse_drift_max_since_peak_sec: Optional[float] = Query(None, ge=300.0, le=86400.0, description="peak 후 최대 시간 초 (default 1800=30분, 이 시간 넘으면 stale → 미발동)"),
    reverse_drift_pct: Optional[float] = Query(None, ge=0.01, le=1.0, description="역행 임계 % (ATR 적응 OFF 또는 floor, default 0.26)"),
    reverse_drift_atr_adaptive_enabled: Optional[bool] = Query(None, description="ATR 기반 적응 임계 ON/OFF (default ON)"),
    reverse_drift_atr_multiplier: Optional[float] = Query(None, ge=0.05, le=1.0, description="atr_pct × multiplier = 임계 (default 0.2)"),
    reverse_drift_atr_cap_pct: Optional[float] = Query(None, ge=0.1, le=2.0, description="적응 임계 상한 (default 0.4)"),
    # ── ★ 끝물 추격 차단 (Overextension) — 2026-06-07 부모 (라이브 ON) ──
    overextension_enabled: Optional[bool] = Query(None, description="끝물 추격 감점 ON/OFF: 24H 범위 상단(LONG)/하단(SHORT)+큰 이동폭 = 소진추세 추격 → conviction 감점 (default ON)"),
    overextension_range_pos_pct: Optional[float] = Query(None, ge=0.5, le=1.0, description="LONG 발동 24H 범위 위치 (default 0.85 = 상단 15%). SHORT 은 1-이값"),
    overextension_min_move_pct: Optional[float] = Query(None, ge=0.0, le=50.0, description="발동 최소 24H 변동 |%| (default 8.0, 작은 변동 제외)"),
    overextension_penalty: Optional[float] = Query(None, ge=0.0, le=50.0, description="conviction 감점 점수 (default 10)"),
    overextension_adx_exempt: Optional[float] = Query(None, ge=0.0, le=100.0, description="ADX 이 이상 = 강한 돌파 → 감점 면제 (default 30, 0=면제없음)"),
    blowoff_filter_enabled: Optional[bool] = Query(None, description="[#1 끝물필터] Blow-off 24h 급등/급락 추격 차단 ON/OFF (default OFF, ADX면제 없음)"),
    blowoff_move_pct: Optional[float] = Query(None, ge=5, le=300, description="[#1] 24h |변동| 이 % 이상=blow-off 후보 (default 30)"),
    blowoff_penalty: Optional[float] = Query(None, ge=0, le=100, description="[#1] 기본 감점 (default 20)"),
    blowoff_extreme_pct: Optional[float] = Query(None, ge=10, le=500, description="[#1] 극단 변동 % (최대감점, default 80)"),
    blowoff_max_penalty: Optional[float] = Query(None, ge=0, le=150, description="[#1] 극단 최대 감점 (default 40)"),
    blowoff_chase_only: Optional[bool] = Query(None, description="[#1] True=추격(같은방향)만 감점, fade 면제 (default True)"),
    # 🎯 변곡 setup 점수 (Inflection Setup) — 2026-06-12 부모 "점수가 차트를 배신"
    inflection_setup_enabled: Optional[bool] = Query(None, description="변곡 setup 점수 ON/OFF: 위치(이동폭)×모멘텀 → 천장stall 롱감점/숏가점, 바닥변곡 롱가점, 벽타기 면제 (default OFF)"),
    inflection_setup_weight: Optional[float] = Query(None, ge=0.0, le=60.0, description="변곡 modifier 최대 크기 W (default 20)"),
    inflection_setup_cap: Optional[float] = Query(None, ge=0.0, le=60.0, description="출력 클램프 ±cap (default 20)"),
    inflection_setup_base: Optional[float] = Query(None, ge=0.0, le=1.0, description="위치만으로 주는 기본 가감 base (default 0.45)"),
    inflection_setup_slope_scale: Optional[float] = Query(None, ge=0.05, le=5.0, description="slope15m tanh 정규화 스케일 % (default 0.40)"),
    # 🎣 Retest setup 점수 (2026-06-12 부모/동생) — 돌파→눌림→지지 = 좋은 진입 자리
    retest_setup_enabled: Optional[bool] = Query(None, description="Retest 가점 ON/OFF: 돌파→되돌림→지지+turning = 좋은 진입 자리 가점 (default OFF)"),
    retest_setup_weight: Optional[float] = Query(None, ge=0.0, le=40.0, description="retest 가점 최대 크기 (default 12)"),
    retest_setup_turn_bonus: Optional[float] = Query(None, ge=0.0, le=20.0, description="되돌림 후 turning 시 추가 가점 (default 4)"),
    retest_retr_lo: Optional[float] = Query(None, ge=0.0, le=1.0, description="최소 되돌림 비율, 이하=천장추격 신호X (default 0.30)"),
    retest_retr_hi: Optional[float] = Query(None, ge=0.0, le=1.5, description="이상적 되돌림 상한, +0.3 초과=too-deep (default 0.90)"),
    # 🌋 변동성 각성 SL 적응 (2026-06-11 부모 "멀게 두고 따라붙기") — SL 넓히면 size 자동↓ 리스크 고정
    awaken_sl_enabled: Optional[bool] = Query(None, description="변동성 각성 SL 적응: 각성+Day순행 시 SL 넓게+size 자동축소(리스크 고정) (default OFF)"),
    awaken_sl_mode: Optional[str] = Query(None, description="SL 거리 기준: atr / structure / both (default both=더 먼 쪽)"),
    awaken_atr_ratio: Optional[float] = Query(None, ge=1.0, le=5.0, description="각성 판정 현재/과거 ATR 비율 (default 1.3)"),
    awaken_atr_lookback: Optional[int] = Query(None, ge=10, le=100, description="과거 ATR 평균 봉수 H4 (default 20)"),
    awaken_max_sl_mult: Optional[float] = Query(None, ge=1.0, le=5.0, description="SL 최대 배수 무한확장 방지 (default 2.5)"),
    awaken_require_day_align: Optional[bool] = Query(None, description="Day(코인 D1) 순행만 견딤 자격 (default True, 역행/미정 제외)"),
    awaken_swing_lookback: Optional[int] = Query(None, ge=3, le=50, description="구조점(각성의 발) swing 탐색 봉수 (default 10)"),
    awaken_atr_buffer: Optional[float] = Query(None, ge=0.0, le=3.0, description="구조점에 ATR 여유 배수 (default 0.5)"),
    # ② 끝물 상한 감점 (2026-06-09 부모 "90+=끝물=50↓, 벽타기 예외")
    conviction_ceiling_enabled: Optional[bool] = Query(None, description="② 끝물 상한 감점: conviction 90+ 를 target 으로 cap (default OFF)"),
    conviction_ceiling_start: Optional[float] = Query(None, ge=50.0, le=150.0, description="이 이상 conviction=끝물 후보 (default 90)"),
    conviction_ceiling_target: Optional[float] = Query(None, ge=0.0, le=100.0, description="끝물을 이 점수로 cap (default 50, 65미달=차단)"),
    conviction_ceiling_adx_exempt: Optional[float] = Query(None, ge=0.0, le=100.0, description="ADX 이 이상=벽타기 면제 (default 30, 0=면제없음)"),
    # ★ 이윤 여력 페널티 (2026-06-09 부모 "방향 맞아도 갈 곳 없으면 감점")
    headroom_penalty_enabled: Optional[bool] = Query(None, description="이윤 여력 페널티: 저항/지지 코앞·RSI 극단·BB 밴드끝 진입 감점 (default OFF)"),
    headroom_sr_penalty: Optional[float] = Query(None, ge=0.0, le=30.0, description="LONG 저항 코앞 / SHORT 지지 코앞 감점 (default 6)"),
    headroom_sr_near_pct: Optional[float] = Query(None, ge=0.1, le=10.0, description="저항/지지까지 이 %이내=여력없음 (default 1.5)"),
    headroom_rsi_penalty: Optional[float] = Query(None, ge=0.0, le=30.0, description="LONG 과매수 / SHORT 과매도 감점 (default 6)"),
    headroom_rsi_overbought: Optional[float] = Query(None, ge=50.0, le=100.0, description="LONG: RSI 이 이상=갈곳없음 (default 70)"),
    headroom_rsi_oversold: Optional[float] = Query(None, ge=0.0, le=50.0, description="SHORT: RSI 이 이하=갈곳없음 (default 30)"),
    headroom_bb_penalty: Optional[float] = Query(None, ge=0.0, le=30.0, description="LONG BB상단 / SHORT BB하단 감점 (default 4)"),
    headroom_bb_hi_pctb: Optional[float] = Query(None, ge=0.5, le=1.5, description="%b 이 이상=밴드상단 (default 0.80)"),
    headroom_bb_lo_pctb: Optional[float] = Query(None, ge=-0.5, le=0.5, description="%b 이 이하=밴드하단 (default 0.20)"),
    # ── 🌊 거시하락 능동 SHORT 진입 2단계 (Macro Short Timing) — 2026-06-11 부모 "물길 완성" ──
    macro_short_timing_enabled: Optional[bool] = Query(None, description="거시하락 2단계: 거시 RISK_OFF + 5m 반등꺾임 = 능동 SHORT 진입(가점). SHORT 전용·끝물방지 (default OFF)"),
    macro_short_timing_delta: Optional[float] = Query(None, ge=0.0, le=40.0, description="SHORT conviction 가점 크기 (default 12)"),
    macro_short_timing_min_signals: Optional[int] = Query(None, ge=1, le=3, description="꺾임 3신호(음전환/MACD<0/PA) 중 최소 충족 수 (default 2)"),
    macro_short_timing_bounce_pct: Optional[float] = Query(None, ge=0.0, le=5.0, description="'반등 존재' 전제 — 5m 저점 후 고점 반등 최소 % (default 0.3)"),
    macro_short_timing_lookback: Optional[int] = Query(None, ge=6, le=40, description="5m 반등 탐색 봉 수 (default 12)"),
    # ── ★ 레짐역행 보유탈출 P3 (2026-06-06 부모) — router 배선 누락 fix (2026-06-07) ──
    macro_exit_enabled: Optional[bool] = Query(None, description="레짐역행 보유탈출 P3: RISK_ON+SHORT / RISK_OFF+LONG 보유분 SL 가까운 출구로 (default OFF, 청산가드)"),
    macro_exit_breadth_min: Optional[int] = Query(None, ge=5, le=10, description="발동 breadth STRONG N/10 (default 8 = 확실할 때만)"),
    macro_exit_sl_cushion_pct: Optional[float] = Query(None, ge=0.05, le=2.0, description="SL 현재가 거리 % (default 0.15, 작을수록 즉시탈출)"),
    macro_exit_strong_coin_exempt: Optional[bool] = Query(None, description="개별 강세 예외: 수익中이면 거시역행이어도 보유유지 (default ON)"),
    macro_exit_exempt_min_roe: Optional[float] = Query(None, ge=0.0, le=20.0, description="예외 최소 가격ROE% (default 0 = 수익이면 무조건 예외)"),
    # ── ★ router 배선 누락 일괄 fix (2026-06-07) — dataclass+UI 있는데 POST 누락이던 12필드 (BB벽타기/레짐컴퍼스P2/final5m/micro1m/multiBE) ──
    bb_block_trend_bypass_adx: Optional[float] = Query(None, ge=0.0, le=100.0, description="BB 벽타기: ADX≥이값=강추세 BB극단차단 우회 (0=비활성)"),
    bb_trend_bypass_require_di: Optional[bool] = Query(None, description="BB 벽타기 ② 방향확정(DI) 필수"),
    bb_trend_bypass_macd_min: Optional[float] = Query(None, ge=0.0, le=10.0, description="BB 벽타기 ③ MACD 모멘텀 허용치 (0=비활성)"),
    final_30m15m_bypass_conviction: Optional[float] = Query(None, ge=0, le=200, description="final_30m15m 점수흡수 — 이 conviction 이상이면 차단 면제 (0=OFF, 예 75)"),
    final_30m15m_bypass_include_regime: Optional[bool] = Query(None, description="거시역행(regime_opposed)도 점수흡수 포함 (True=포함/False=제외·기존)"),
    final_d1_bypass_conviction: Optional[float] = Query(None, ge=0, le=200, description="D1 점수흡수 — 이 conviction 이상이면 D1 역행 차단 면제 (0=OFF, 예 78). 출구가드가 받침"),
    final_d1_recent5_override_enabled: Optional[bool] = Query(None, description="final_d1 최근5봉 override — D1=UPTREND 오판(lookback=5 잔상) 시 최근 5일봉이 명확히 DOWN이면 SHORT 통과 (default OFF)"),
    final_d1_recent5_drop_pct: Optional[float] = Query(None, ge=0, le=50, description="최근 5일봉 변화율 ≤ -이값(%) 이면 UPTREND 라벨 무시 SHORT 통과 (예 1.0)"),
    d1_reality_demote_enabled: Optional[bool] = Query(None, description="Fix D — D1 추세 라벨 reality check: UPTREND인데 최근 5일봉 ≤ -drop% 면 SIDEWAYS 강등(추세정렬 LONG credit 제거). 떨어지는칼 LONG 차단·카드 라벨 교정. default OFF"),
    d1_reality_demote_drop_pct: Optional[float] = Query(None, ge=0, le=50, description="최근 5일봉 변화율 ≤ -이값(%) 이면 UPTREND→SIDEWAYS 강등 (예 1.0)"),
    guard_score_total_cap_enabled: Optional[bool] = Query(None, description="[패치 v1] 가드 가산점 총합 캡 ON/OFF (default OFF)"),
    guard_score_total_cap: Optional[float] = Query(None, ge=5, le=100, description="[패치 v1] 총합 클램프 ±N (default 30)"),
    conviction_ceiling_post_guards: Optional[bool] = Query(None, description="[패치 v1] 끝물 상한을 base+가드 합산 후 적용 (default OFF)"),
    final_bypass_use_base: Optional[bool] = Query(None, description="[패치 v1] 점수흡수 bypass 를 base conviction 기준으로 (default OFF)"),
    final_5m_simple_check_enabled: Optional[bool] = Query(None, description="진입 직전 5M RSI/MACD/BB 동조 검사"),
    final_5m_simple_min_score: Optional[int] = Query(None, ge=0, le=3, description="5M 3종 중 N 이상 동조 시 통과"),
    final_5m_bb_trend_bypass_enabled: Optional[bool] = Query(None, description="final_5m BB 벽타기 면제 — 강한추세(ADX+DI)면 BB 극단(SHORT 바닥/LONG 천장)이어도 통과. default OFF"),
    final_d1_alignment_check_enabled: Optional[bool] = Query(None, description="D1 정렬 필수 — Day 캔들 역방향 시 진입차단 (OFF=이벤트로 흔들린 Day캔들 무시, 2026-06-07 운영자)"),
    final_align_regime_override_enabled: Optional[bool] = Query(None, description="거시 정렬 override — 급락(RISK_OFF) 확실 시 final게이트가 상위TF 대신 거시방향 따름 (SHORT순행 통과/LONG떨어지는칼 차단, 2026-06-07)"),
    macro_compass_enabled: Optional[bool] = Query(None, description="레짐 컴퍼스 P2 (RECOVERING 가점, default OFF paper)"),
    macro_recovering_conv_delta: Optional[float] = Query(None, ge=-50.0, le=50.0, description="RECOVERING LONG 가점/SHORT 감점 폭 (0=paper)"),
    macro_recovering_require_di_adx: Optional[bool] = Query(None, description="죽은고양이 방어: +DI flip+ADX 동반만 가점"),
    macro_recovering_min_adx: Optional[float] = Query(None, ge=0.0, le=100.0, description="회복 확인 최소 ADX"),
    micro_1m_body_min_pct: Optional[float] = Query(None, ge=0.0, le=5.0, description="1M 약한 도지 거름 — 진짜 미는 봉 최소 body %"),
    multi_be_lock_atr_adaptive_enabled: Optional[bool] = Query(None, description="멀티 BE락 ATR 배수 모드 ON/OFF"),
    multi_be_lock_atr_min_stage1_trigger_pct: Optional[float] = Query(None, ge=0.0, le=5.0, description="멀티 BE락 stage1 트리거 floor %"),
    multi_be_lock_atr_max_stage1_trigger_pct: Optional[float] = Query(None, ge=0.0, le=20.0, description="[2026-06-13] 멀티 BE락 stage1 트리거 상한 % — extreme ATR도 +N%엔 BE 락 (0=상한없음, default 3.0)"),
    # ── ★ Entry Grace Period (2026-05-18 부모 비전 #6) — 진입 후 분위기 파악 시간 ──
    entry_grace_period_sec: Optional[float] = Query(None, ge=0.0, le=3600.0, description="진입 후 N초 동안 pre_be_stall + reverse_drift 가드 비활성 (A/B 판별 시간 벌기). 0=OFF, 300=5분 권장. be_stall/SL/long_hold 영향 X."),
    market_bias_grace_exit_enabled: Optional[bool] = Query(None, description="[운영자 비전 #6 보조] 그레이스 기간 중 market_bias 반대 dominance 감지 시 즉시 force exit (A 패턴 회피). default OFF. entry_grace_period_sec 와 함께 켜야 발동."),
    news_grace_exit_enabled: Optional[bool] = Query(None, description="[운영자 비전 #6 보조 — 뉴스 부활] 그레이스 기간 중 뉴스 sentiment 반대 강 시 force exit. default OFF. news_sentiment.focus_enabled (/api/news-sentiment/config) 도 함께 켜야 발동."),
    news_grace_exit_threshold: Optional[float] = Query(None, ge=0.1, le=1.0, description="[운영자 비전 #6 보조] news_grace_exit 발동 임계 |sentiment| (default 0.5)"),
    # ── ★★★★ [2026-05-18 부모 비전 #6 B 옵션] 시간 무관 OR 조건 ──
    exit_consensus_enabled: Optional[bool] = Query(None, description="[운영자 비전 #6 B 옵션] 시간 무관 OR 조건. reverse_drift/pre_be_stall 발동 시 옆친구+뉴스 의견 종합. 같은방향=hold(견디기) / 반대=exit(가드따름). 시간 그레이스 없이 작동. default OFF."),
    exit_consensus_news_threshold: Optional[float] = Query(None, ge=0.1, le=1.0, description="[운영자 비전 #6 B 옵션] exit_consensus 뉴스 sentiment 강도 임계 (default 0.3 완만)"),
    # ── Long Hold Timeout (3-tier, 2026-04-25) ──
    long_hold_timeout_enabled: Optional[bool] = Query(None, description="Long Hold Timeout (3-tier) ON/OFF"),
    long_hold_timeout_tier1_min: Optional[float] = Query(None, ge=0, le=99999, description="Tier1: 시간(분) — 0=비활성, 9999=사실상 OFF"),
    long_hold_timeout_tier1_peak_pct: Optional[float] = Query(None, ge=0.01, le=2.0, description="Tier1: peak < 임계(%) 시 컷"),
    long_hold_timeout_tier2_min: Optional[float] = Query(None, ge=0, le=99999, description="Tier2: 시간(분) — 0=비활성, 9999=사실상 OFF"),
    long_hold_timeout_tier2_peak_pct: Optional[float] = Query(None, ge=0.01, le=2.0, description="Tier2: peak < 임계(%) 시 컷"),
    long_hold_timeout_tier3_min: Optional[float] = Query(None, ge=0, le=99999, description="Tier3: 시간(분) — BE-distant 컷 (default 30, 9999=OFF)"),
    long_hold_timeout_tier3_peak_pct: Optional[float] = Query(None, ge=0.01, le=2.0, description="Tier3: peak < 임계(%) 시 컷 (default 0.2)"),
    # ── ★ Entry Expectation (2026-05-14 부모 — 진입 기대치 메커니즘) ──
    entry_expectation_enabled: Optional[bool] = Query(None, description="진입 시 primary_tf(H1) 구조로 reward/risk 산정 + 거래소 TP/SL 일원화 ON/OFF"),
    expectation_progress_exit_enabled: Optional[bool] = Query(None, description="진행률 기반 청산 (LHT 시간컷 대체) ON/OFF"),
    expectation_progress_t1_min: Optional[float] = Query(None, ge=1, le=600, description="진행률 컷 T1: N분 경과 (default 15)"),
    expectation_progress_t1_pct: Optional[float] = Query(None, ge=0, le=100, description="진행률 컷 T1: 목표 진행률 < M% 면 컷 (default 30)"),
    expectation_progress_t2_min: Optional[float] = Query(None, ge=1, le=600, description="진행률 컷 T2: N분 경과 (default 30)"),
    expectation_progress_t2_pct: Optional[float] = Query(None, ge=0, le=100, description="진행률 컷 T2: 목표 진행률 < M% 면 컷 (default 50)"),
    # ── ★ 음수 progress 즉시 컷 (2026-05-15 부모) ──
    expectation_progress_neg_cut_enabled: Optional[bool] = Query(None, description="음수 progress 즉시 컷 (손실 방향 명백시 빠른 컷)"),
    expectation_progress_neg_cut_pct: Optional[float] = Query(None, ge=-1000.0, le=0.0, description="진행률 임계 (음수, default -50 = 목표 반대로 50% 진행)"),
    expectation_progress_neg_cut_min: Optional[float] = Query(None, ge=1, le=600, description="음수 컷 최소 보유시간(분, default 30)"),
    # ── ★ Entry Quality Gates (2026-05-15 부모) ──
    entry_expectation_gate_enabled: Optional[bool] = Query(None, description="#1 RR/risk 게이트: 임계 미달 진입 차단 ON/OFF (entry_expectation_enabled 필요)"),
    entry_expectation_min_rr: Optional[float] = Query(None, ge=0, le=10, description="RR floor — 이 값 미만이면 차단 (default 1.0, 운영자 운영 완화)"),
    entry_expectation_min_reward_pct: Optional[float] = Query(None, ge=0, le=10, description="reward_pct floor — 예상 도달 %가 이 값 미만이면 차단 (default 0.8, 5-14 설계도 Gate 2 명세)"),
    entry_expectation_max_risk_pct: Optional[float] = Query(None, ge=0.5, le=30, description="risk_pct cap — 이 값(%) 초과면 차단 (default 6.0, 5/15 SIREN 사고 안전망)"),
    # ── 🌍 [2026-06-02 거시 레짐 방향 게이트] Market Breadth (대표10 쓰나미) ──
    breadth_strong_n: Optional[int] = Query(None, ge=1, le=10, description="STRONG 임계 N/10 (default 8). N개 코인 일제=강한 쓰나미"),
    breadth_mid_n: Optional[int] = Query(None, ge=1, le=10, description="MID 임계 N/10 (default 6)"),
    breadth_aligned_strong: Optional[float] = Query(None, ge=0, le=100, description="순행 STRONG 가점 (default 12, 흐름따름=기회)"),
    breadth_aligned_mid: Optional[float] = Query(None, ge=0, le=100, description="순행 MID 가점 (default 6)"),
    breadth_counter_strong: Optional[float] = Query(None, ge=-100, le=0, description="역행 STRONG 감점 (default -25, 떨어지는칼=차단)"),
    breadth_counter_mid: Optional[float] = Query(None, ge=-100, le=0, description="역행 MID 감점 (default -7)"),
    regime_counter_strong_cap_enabled: Optional[bool] = Query(None, description="STRONG 역행 시 conviction cap ON/OFF (떨어지는칼 점수 강제하향)"),
    regime_counter_strong_cap: Optional[float] = Query(None, ge=0, le=100, description="STRONG 역행 conviction cap 값 (default 50)"),
    regime_short_release_enabled: Optional[bool] = Query(None, description="SHORT 해방 — 거시 하락 시 SHORT 순행 통과 (두 다리) ON/OFF"),
    regime_short_release_n: Optional[int] = Query(None, ge=1, le=10, description="SHORT 통과 거시 하락 코인수 (default 6, MID임계 이하로 RISK_OFF 시 SHORT 해방)"),
    # ── 🦵 [2026-06-11] 개별 코인 디커플링 SHORT 해방 ──
    coin_decouple_enabled: Optional[bool] = Query(None, description="개별 디커플링 SHORT 해방 — BTC와 반대로 무너진 코인에 약자 다리 해방 ON/OFF (default OFF)"),
    coin_decouple_short_release: Optional[float] = Query(None, ge=0, le=60, description="디커플링 시 코인 구조방향 가점 (btc -20 구멍 상쇄, default 22)"),
    coin_decouple_long_penalty: Optional[float] = Query(None, ge=0, le=60, description="디커플링 시 역행 다리(떨어지는칼) 페널티 (default 12)"),
    coin_decouple_min_strength: Optional[float] = Query(None, ge=0, le=1, description="코인 6TF 확신도 최소 (default 0.5, 흔들림 제외)"),
    coin_decouple_btc_cache_sec: Optional[float] = Query(None, ge=10, le=600, description="BTC 6TF 방향 캐시 TTL초 (default 120)"),
    # ── 🦵🌊 [2026-06-12 부모] 모멘텀 decouple — coin_decouple 의 선행 버전 (변곡 up 검출, conviction 해방) ──
    mom_decouple_enabled: Optional[bool] = Query(None, description="모멘텀 decouple — 천장서 코인 혼자 모멘텀死 시 약자다리 conviction 해방 ON/OFF (default OFF)"),
    mom_decouple_weight: Optional[float] = Query(None, ge=0, le=60, description="conviction 가감 스케일 W (default 30, 50점격차 flip)"),
    mom_decouple_cap: Optional[float] = Query(None, ge=0, le=60, description="출력 클램프 ±cap (default 35)"),
    mom_decouple_base: Optional[float] = Query(None, ge=0, le=1, description="위치만의 기본 가감 base (default 0.45)"),
    mom_decouple_up_thr: Optional[float] = Query(None, ge=0, le=1, description="모멘텀 |up| 최소 — 이하면 꺾임 아님 (default 0.40)"),
    mom_decouple_div_thr: Optional[float] = Query(None, ge=0, le=2, description="BTC 모멘텀 대비 발산 최소 — 시장 동반눌림 제외 (default 0.20)"),
    mom_decouple_pos_hi: Optional[float] = Query(None, ge=0, le=1, description="SHORT 해방 위치 하한(천장) (default 0.60)"),
    mom_decouple_pos_lo: Optional[float] = Query(None, ge=0, le=1, description="LONG 해방 위치 상한(바닥) (default 0.40)"),
    mom_decouple_btc_cache_sec: Optional[float] = Query(None, ge=10, le=600, description="BTC 5m 모멘텀 캐시 TTL초 (default 60)"),
    # ── 🔄 [2026-06-02 Phase 3] M/W/H&S 반전 점수 ──
    reversal_score: Optional[float] = Query(None, ge=0, le=50, description="반전(M/W/H&S) 점수 (default 10). 순행+/역행−, 형성중 ×0.5"),
    # ── 🕯️ [2026-06-03 부모] TF 추세 가중 (H4/H1/30M/15M/5M) ──
    h4_trend_weight: Optional[float] = Query(None, ge=0, le=3, description="H4(4시간봉) 추세 가중 (default 1.0, ×6=최대)"),
    h1_trend_weight: Optional[float] = Query(None, ge=0, le=3, description="H1 추세 가중 (default 1.0)"),
    m30_trend_weight: Optional[float] = Query(None, ge=0, le=3, description="30M 추세 가중 (default 1.0)"),
    m15_trend_weight: Optional[float] = Query(None, ge=0, le=3, description="15M 추세 가중 (default 1.0)"),
    m5_trend_weight: Optional[float] = Query(None, ge=0, le=3, description="5M 추세 가중 (default 1.0, 0=끔)"),
    breadth_dir_chg1h_pct: Optional[float] = Query(None, ge=0.05, le=2.0, description="breadth 방향판정 1h 변화율 임계 % (default 0.3)"),
    breadth_dir_ema_pct: Optional[float] = Query(None, ge=0.02, le=1.0, description="breadth 방향판정 5분EMA 임계 % (default 0.10)"),
    # [2026-05-23 부모] 변동성 도달가능성 게이트 — "충분한 등락폭으로 TP 갈 수 있나"
    entry_volatility_gate_enabled: Optional[bool] = Query(None, description="변동성 도달가능성 게이트 ON/OFF. reward 거리(저항선)는 멀어도 변동성 죽으면 못 감 — 횡보 죽은 자리 차단"),
    entry_volatility_lookback_tf: Optional[str] = Query(None, description="등락폭 측정 TF (default 5분봉)"),
    entry_volatility_lookback_bars: Optional[int] = Query(None, ge=3, le=100, description="최근 N봉 등락폭 측정 (default 12 = 1시간)"),
    entry_volatility_min_reach_ratio: Optional[float] = Query(None, ge=0.1, le=3.0, description="최근등락폭/reward거리 ≥ 이 비율이어야 진입 (default 0.6)"),
    entry_flip_require_alignment: Optional[bool] = Query(None, description="#2 FLIP alignment: FLIP 방향이 H1+30M 둘 다 반대면 차단 ON/OFF"),
    # ── ★ Long Hold Persistence (2026-04-26 부모님 "이윤 못내면 못나가") ──
    trend_reversal_enabled: Optional[bool] = Query(None, description="추세 반전 자동 청산 ON/OFF"),
    bb_macd_sw_enabled: Optional[bool] = Query(None, description="SIDEWAYS BB+MACD 자동 청산 ON/OFF"),
    bb_macd_sw_min_hold_hours: Optional[float] = Query(None, ge=0.1, le=99.0, description="bb_macd_sw 발동 최소 보유(h)"),
    bb_macd_sw_pnl_low: Optional[float] = Query(None, ge=-99.0, le=0.0, description="bb_macd_sw 발동 pnl 하한(%)"),
    bb_macd_sw_pnl_high: Optional[float] = Query(None, ge=0.0, le=99.0, description="bb_macd_sw 발동 pnl 상한(%)"),
    caution_sideways_profit_secure_enabled: Optional[bool] = Query(None, description="횡보+이윤 자동 익절 ON/OFF"),
    caution_min_hold_sec: Optional[float] = Query(None, ge=0, le=86400, description="caution 발동 최소 보유(초)"),
    caution_fee_rate: Optional[float] = Query(None, ge=0.0, le=0.01, description="caution 수수료율"),
    caution_min_profit_multiplier: Optional[float] = Query(None, ge=0.1, le=100.0, description="caution 최소 순이익 = 수수료 × N"),
    quick_tp_enabled: Optional[bool] = Query(None, description="시간 기반 빠른 TP ON/OFF"),
    quick_tp_min_hold_hours: Optional[float] = Query(None, ge=0.1, le=999.0, description="quick_tp 발동 최소 보유(h)"),
    quick_tp_min_pnl_pct: Optional[float] = Query(None, ge=0.0, le=99.0, description="quick_tp 발동 최소 pnl(%)"),
    btc_crash_threshold_pct: Optional[float] = Query(None, ge=-99.0, le=0.0, description="BTC 급락 자동 청산 임계(%)"),
    btc_emergency_pause_enabled: Optional[bool] = Query(None, description="BTC 급변동 감지 ON/OFF"),
    btc_emergency_pause_threshold_pct: Optional[float] = Query(None, ge=0.5, le=99.0, description="발동 임계 (절대값 %, default 5)"),
    btc_emergency_pause_window_min: Optional[float] = Query(None, ge=1.0, le=120.0, description="체크 윈도우 (분, default 10)"),
    btc_emergency_mode: Optional[str] = Query(None, description="모드: trend_aligned/pause/close_all"),
    btc_emergency_aggressive_entry: Optional[bool] = Query(None, description="빈 슬롯 트렌드 방향 진입 가속 ON/OFF"),
    btc_emergency_aligned_duration_min: Optional[float] = Query(None, ge=1.0, le=1440.0, description="트렌드 정렬 유지 시간 (분, default 120=2h)"),
    # ★ [2026-04-26] Winners-Only Add — 부모님 "진정한 Autocoin"
    winners_add_enabled: Optional[bool] = Query(None, description="Winners Add ON/OFF — 자본 추가 시 유리한 코인 증액"),
    winners_add_capital_threshold_pct: Optional[float] = Query(None, ge=1.0, le=99.0, description="발동 임계 (equity +N% 증가)"),
    winners_add_min_pnl_pct: Optional[float] = Query(None, ge=0.0, le=99.0, description="1순위 pnl 임계 (%)"),
    winners_add_max_per_event: Optional[int] = Query(None, ge=1, le=10, description="한 번 발동 최대 코인 수"),
    winners_add_max_pct_per_coin: Optional[float] = Query(None, ge=1.0, le=999.0, description="코인당 max 추가 = 기존 margin × N%"),
    winners_add_cooldown_sec: Optional[float] = Query(None, ge=60, le=86400, description="발동 cooldown (초)"),
    min_sl_pct: Optional[float] = Query(None, ge=0.0001, le=0.5, description="SL 최소 거리 (가격 비율, 0.001=0.1%)"),
    max_sl_distance_pct: Optional[float] = Query(None, ge=0.5, le=99.9, description="SL 최대 거리 (%, 99=사실상 비활성)"),
    max_atr_pct: Optional[float] = Query(None, ge=0.5, le=99.0, description="ATR cap (%, 변동성 큰 코인 보호)"),
    cycle_min_rr: Optional[float] = Query(None, ge=0.1, le=10.0, description="TP/SL 최소 RR (1.0=가드 비활성)"),
    # ── Min TP fee-guard (2026-05-15 부모, 진입 직후 즉시 TP hit + 수수료 손실 방지) ──
    min_tp_distance_enabled: Optional[bool] = Query(None, description="Min TP fee-guard: 진입가 옆 TP 금지 (저변동 코인 보호)"),
    min_tp_distance_pct: Optional[float] = Query(None, ge=0.05, le=2.0, description="Min TP 거리 (%, 수수료 왕복 0.11%×~3=0.30)"),
    # ── 5m Microtiming Gate (2026-05-16 부모, "BLOCK 말고 WAIT — 정확한 자리에 들어가게") ──
    microtiming_5m_enabled: Optional[bool] = Query(None, description="5m RSI/MACD/BB 마이크로 타이밍 게이트 (BLOCK 아닌 defer)"),
    microtiming_5m_min_score: Optional[int] = Query(None, ge=1, le=3, description="3종 중 N개 충족 시 진입 (default 2)"),
    microtiming_5m_defer_sec: Optional[float] = Query(None, ge=60.0, le=3600.0, description="defer 후 재평가 간격 (s)"),
    microtiming_5m_max_defers: Optional[int] = Query(None, ge=1, le=10, description="최대 defer 회수 (초과시 자연 만료)"),
    microtiming_5m_rsi_long_threshold: Optional[float] = Query(None, ge=10.0, le=50.0, description="LONG: 직전 RSI ≤ 이 값 + 상승 변곡"),
    microtiming_5m_rsi_short_threshold: Optional[float] = Query(None, ge=50.0, le=90.0, description="SHORT: 직전 RSI ≥ 이 값 + 하강 변곡"),
    microtiming_5m_bb_low_pct: Optional[float] = Query(None, ge=0.0, le=50.0, description="BB 하단권 임계 % (LONG 직전 위치)"),
    microtiming_5m_bb_recover_pct: Optional[float] = Query(None, ge=0.0, le=80.0, description="BB 회복 임계 % (LONG 현재 위치)"),
    microtiming_5m_phase_k_exempt: Optional[bool] = Query(None, description="Phase K (regime transition) 진입 면제"),
    # ── DrawdownShield base (2026-05-16 부모, 미실현 변동이 다른 진입 막는 문제 해결) ──
    drawdown_shield_use_cash_only: Optional[bool] = Query(None, description="DrawdownShield: True=cash만 (UPL 무시), False=equity (UPL 포함, 기존)"),
    drawdown_shield_caution_pct: Optional[float] = Query(None, ge=0, le=100, description="DrawdownShield 누적 CAUTION 임계 (%, default 5)"),
    drawdown_shield_defend_pct: Optional[float] = Query(None, ge=0, le=100, description="누적 DEFEND 임계 (%, default 10)"),
    drawdown_shield_crisis_pct: Optional[float] = Query(None, ge=0, le=100, description="누적 CRISIS 임계 (%, default 20)"),
    drawdown_shield_caution_usd: Optional[float] = Query(None, ge=0, le=100000, description="일간 CAUTION 임계 ($, default 30)"),
    drawdown_shield_defend_usd: Optional[float] = Query(None, ge=0, le=100000, description="일간 DEFEND 임계 ($, default 60)"),
    drawdown_shield_crisis_usd: Optional[float] = Query(None, ge=0, le=100000, description="일간 CRISIS 임계 ($, default 100)"),
    drawdown_shield_caution_pen: Optional[float] = Query(None, ge=-100, le=0, description="CAUTION conviction penalty (음수, default -10)"),
    drawdown_shield_defend_pen: Optional[float] = Query(None, ge=-100, le=0, description="DEFEND penalty (default -20)"),
    drawdown_shield_crisis_pen: Optional[float] = Query(None, ge=-100, le=0, description="CRISIS penalty (default -30)"),
    # ── [2026-05-16 부모] Same-coin Flip Cooldown + 5m Raw Body Guard + Imminent Flip ──
    same_coin_flip_cooldown_enabled: Optional[bool] = Query(None, description="같은 코인 LONG↔SHORT 신규 진입 N분 cooldown ON/OFF"),
    same_coin_flip_cooldown_min: Optional[int] = Query(None, ge=0, le=600, description="cooldown 분 (60=기본)"),
    # ── ★ [2026-06-05 부모] 1M 마이크로 체크 ──
    micro_1m_check_enabled: Optional[bool] = Query(None, description="1M 마이크로 체크 ON/OFF — 진입 직전 1분봉 타이밍 검증"),
    micro_1m_candle_check: Optional[bool] = Query(None, description="① 마지막 1M 봉 방향 체크"),
    micro_1m_candle_trend_exempt_adx: Optional[float] = Query(None, ge=0, le=100, description="추세 강하면(ADX≥이값=벽타기) 1M 봉 방향 면제 → 진입 지연 방지 (0=비활성, 예 30)"),
    micro_1m_volume_check: Optional[bool] = Query(None, description="② 1M 거래량 연속 감소 체크"),
    micro_1m_rsi_check: Optional[bool] = Query(None, description="③ 1M RSI 극단 체크"),
    micro_1m_rsi_long_max: Optional[float] = Query(None, ge=50, le=90, description="LONG RSI 과열 임계 (기본 70)"),
    micro_1m_rsi_short_min: Optional[float] = Query(None, ge=10, le=50, description="SHORT RSI 과열 임계 (기본 30)"),
    micro_1m_vol_decline_bars: Optional[int] = Query(None, ge=2, le=10, description="거래량 연속 감소 봉수 (기본 3)"),
    raw_body_guard_enabled: Optional[bool] = Query(None, description="5m raw body 가드 ON/OFF — 최근 N봉 시가→종가 net 부호 반대면 BLOCK"),
    raw_body_guard_lookback: Optional[int] = Query(None, ge=1, le=20, description="lookback 5m 봉 수 (3=기본)"),
    raw_body_guard_min_net_pct: Optional[float] = Query(None, ge=0.0, le=5.0, description="min net % (0=부호만, 0.15~0.30 권장)"),
    # ── [2026-05-16 부모 비전] Momentum Derivative Guard (RSI/MACD 흐름 1차 미분) ──
    momentum_deriv_guard_enabled: Optional[bool] = Query(None, description="RSI/MACD hist 변화율 가드 ON/OFF — 진입 방향과 반대 흐름이면 BLOCK"),
    momentum_deriv_guard_tf: Optional[str] = Query(None, description="TF (1/5/15/30/60), 기본 5"),
    momentum_deriv_guard_lookback: Optional[int] = Query(None, ge=2, le=50, description="비교 윈도우 봉 수 (5=기본)"),
    momentum_deriv_guard_rsi_min_slope: Optional[float] = Query(None, ge=0.0, le=50.0, description="RSI Δ 임계 (절대, 2.0=기본)"),
    momentum_deriv_guard_macd_min_slope: Optional[float] = Query(None, ge=0.0, le=10.0, description="MACD hist Δ 임계 (0=부호만)"),
    momentum_deriv_guard_require_both: Optional[bool] = Query(None, description="True=RSI+MACD 둘 다 반대여야 BLOCK, False=하나만 반대여도 BLOCK"),
    # ── [2026-05-16 부모 비전 #2] MTF Momentum Alignment (TF 들 가속 일관성) ──
    mtf_momentum_align_enabled: Optional[bool] = Query(None, description="MTF 모멘텀 정렬 가드 ON/OFF — TF 들 가속 방향이 진입 방향과 일치하는지"),
    mtf_momentum_align_tfs: Optional[str] = Query(None, description="TFs CSV (예: '60,30,5')"),
    mtf_momentum_align_lookback: Optional[int] = Query(None, ge=2, le=50, description="각 TF 비교 윈도우 봉 수"),
    mtf_momentum_align_min_aligned: Optional[int] = Query(None, ge=1, le=10, description="최소 일치 TF 수 (3개 중 2개 등)"),
    mtf_momentum_align_rsi_slope_thr: Optional[float] = Query(None, ge=0.0, le=20.0, description="RSI Δ 부호 판정 임계"),
    mtf_momentum_align_use_macd: Optional[bool] = Query(None, description="True=RSI+MACD 둘 다 일치해야 TF aligned"),
    # ── [2026-05-16 부모 비전 #3] CFID — Coin Flip Imminent Detector ──
    cfid_enabled: Optional[bool] = Query(None, description="코인별 변곡점 임박 감지 ON/OFF"),
    cfid_tf: Optional[str] = Query(None, description="TF (60=H1, 30=30M 권장)"),
    cfid_ema_gap_thr_pct: Optional[float] = Query(None, ge=0.05, le=5.0, description="EMA20-50 gap/price*100 임계"),
    cfid_volume_spike_ratio: Optional[float] = Query(None, ge=1.0, le=10.0, description="최근 N봉 vol avg / 이전 N봉 spike 비율"),
    cfid_adx_change_min: Optional[float] = Query(None, ge=0.1, le=20.0, description="ADX 변화율 절댓값 임계"),
    cfid_lookback: Optional[int] = Query(None, ge=3, le=50, description="비교 윈도우 봉 수"),
    cfid_bypass_momentum_deriv: Optional[bool] = Query(None, description="momentum_deriv 가드 우회 허용"),
    cfid_bypass_mtf_align: Optional[bool] = Query(None, description="mtf_momentum_align 가드 우회 허용"),
    # ── ★ [2026-05-18 부모 비전 #5] Leading Entry — 선행 진입 ──
    leading_entry_mode: Optional[str] = Query(None, description="선행 진입 모드: 'OFF' / 'CFID' / 'PATTERN' (mutually exclusive)"),
    cfid_leading_min_strength: Optional[float] = Query(None, ge=10.0, le=100.0, description="[CFID 모드] CFID strength 임계 (default 70)"),
    cfid_leading_size_pct: Optional[float] = Query(None, ge=0.5, le=50.0, description="[CFID 모드] 진입 사이즈 % of equity (default 5)"),
    cfid_leading_bypass_microtiming: Optional[bool] = Query(None, description="[CFID 모드] 5m microtiming gate 우회"),
    cfid_leading_bypass_bb_regime: Optional[bool] = Query(None, description="[CFID 모드] BB_REGIME 정점/저점 차단 우회"),
    pattern_leading_size_pct: Optional[float] = Query(None, ge=0.5, le=50.0, description="[PATTERN 모드] 진입 사이즈 % of equity (default 5)"),
    pattern_leading_min_5step_score: Optional[int] = Query(None, ge=1, le=12, description="[PATTERN 모드] 5step 12점 만점 중 임계 (default 6)"),
    pattern_leading_max_sr_pct: Optional[float] = Query(None, ge=0.1, le=10.0, description="[PATTERN 모드] sr_near_S/R 거리 % (default 1.0)"),
    pattern_leading_min_mtf_align: Optional[int] = Query(None, ge=1, le=4, description="[PATTERN 모드] mtf_align 정렬 TF 수 (default 2)"),
    pattern_leading_bypass_microtiming: Optional[bool] = Query(None, description="[PATTERN 모드] 5m microtiming gate 우회"),
    pattern_leading_bypass_bb_regime: Optional[bool] = Query(None, description="[PATTERN 모드] BB_REGIME 정점/저점 차단 우회"),
    # ── ★ [2026-05-19 Phase 6 Step 2 B-Full] Combinatorial Weighting ──
    #   조합 4개 (A/B/C/D) 의 가산점 + 발동 임계. 프리셋/UI 에서 조정 가능.
    phase6_combo_a_bonus: Optional[int] = Query(None, ge=0, le=50, description="[조합 A] PA+zone+MTF 정렬 시 가산 (default 25)"),
    phase6_combo_a_sr_min: Optional[int] = Query(None, ge=0, le=10, description="[조합 A] sr_s 최소 (8=near only, 5=mid 까지, default 5)"),
    phase6_combo_a_mtf_min: Optional[int] = Query(None, ge=0, le=4, description="[조합 A] mtf_s 최소 (4=강 정렬, 2=부분 정렬, default 2)"),
    phase6_combo_b_bonus: Optional[int] = Query(None, ge=0, le=70, description="[조합 B] CFID+EMA+vol 시 가산 (default 35)"),
    phase6_combo_b_strength_min: Optional[int] = Query(None, ge=0, le=100, description="[조합 B] CFID strength 최소 (70=강, 50=중간, default 50)"),
    phase6_combo_c_bonus: Optional[int] = Query(None, ge=0, le=40, description="[조합 C] 5step+vol 시 가산 (default 15)"),
    phase6_combo_c_5step_min: Optional[int] = Query(None, ge=0, le=12, description="[조합 C] 5step score 최소 (10=만점, 7=강자리, default 7)"),
    phase6_combo_d_bonus: Optional[int] = Query(None, ge=0, le=40, description="[조합 D] news strong+aligned 시 가산 (default 15)"),
    phase6_combo_d_news_abs_min: Optional[int] = Query(None, ge=0, le=20, description="[조합 D] |news_raw| 최소 (10=강, 6=중간, default 6)"),
    # ── ★ [2026-05-19 부모 결정] BB 차단 가드 임계 (UI 조정 가능, ORDI 교훈 보존) ──
    bb_block_threshold_pct: Optional[float] = Query(None, ge=50.0, le=100.0, description="[BB hardblock] LONG > 이값 차단 (default 85, SHORT 대칭 < 100-이값)"),
    bb_penalty_threshold_pct: Optional[float] = Query(None, ge=50.0, le=100.0, description="[BB 감점] LONG > 이값 conv 감점 (default 75, SHORT 대칭 < 100-이값)"),
    bb_penalty_amount: Optional[float] = Query(None, ge=0.0, le=50.0, description="[BB 감점량] 100점 단위 감점 (default 20)"),
    # ── [2026-05-16 부모 비전 #4] Coin State Machine ──
    coin_state_machine_enabled: Optional[bool] = Query(None, description="진입 시점 코인 상태 4단계 분류 ON/OFF"),
    coin_state_apply_conv_adjust: Optional[bool] = Query(None, description="True=conviction_score 에 단계별 보정 적용 (default OFF)"),
    coin_state_accel_conv_adj: Optional[float] = Query(None, ge=-30, le=30, description="[100점] ACCEL 보정 (default 0)"),
    coin_state_steady_conv_adj: Optional[float] = Query(None, ge=-30, le=30, description="[100점] STEADY 보정 (default -5)"),
    coin_state_decel_conv_adj: Optional[float] = Query(None, ge=-30, le=30, description="[100점] DECEL 보정 (default -10)"),
    coin_state_flip_imminent_conv_adj: Optional[float] = Query(None, ge=-30, le=30, description="[100점] FLIP_IMMINENT 보정 (default +5)"),
    # ── [2026-05-16 부모 비전 #5] Tight Trail After BE ──
    tight_trail_after_be_enabled: Optional[bool] = Query(None, description="BE 락 활성 시 peak slippage 즉시 컷"),
    tight_trail_max_slippage_pct: Optional[float] = Query(None, ge=0.05, le=5.0, description="peak 에서 N%p 빠지면 컷 (0.2=기본)"),
    tight_trail_min_peak_pct: Optional[float] = Query(None, ge=0.1, le=10.0, description="peak 이 이상일 때만 적용 (0.4=기본)"),
    tight_trail_atr_adaptive_enabled: Optional[bool] = Query(None, description="ATR 비례 동적 slippage 임계"),
    tight_trail_atr_tf: Optional[str] = Query(None, description="ATR TF (5=5m 기본)"),
    tight_trail_atr_period: Optional[int] = Query(None, ge=5, le=50, description="ATR period (14=기본)"),
    tight_trail_atr_multiplier: Optional[float] = Query(None, ge=0.05, le=2.0, description="atr_pct × N → slippage (0.3=기본)"),
    tight_trail_atr_cap_pct: Optional[float] = Query(None, ge=0.1, le=5.0, description="adaptive slippage 상한 (0.6=기본)"),
    # ── 🎯 [2026-06-12 부모 ESPORTS/WLD] 코인별 추세-적응 출구 ──
    trend_adaptive_exit_enabled: Optional[bool] = Query(None, description="코인별 추세-적응 출구 — runner 태우고 chopper 스캘프 (ADX 기반, default OFF)"),
    trend_adaptive_exit_adx_strong: Optional[float] = Query(None, ge=10, le=60, description="ADX 이상=runner 트레일 완화 (default 30)"),
    trend_adaptive_exit_adx_weak: Optional[float] = Query(None, ge=5, le=40, description="ADX 이하=chopper 트레일 강화 (default 18)"),
    trend_adaptive_exit_runner_factor: Optional[float] = Query(None, ge=0.1, le=1.0, description="runner factor <1 (preserve↓/slip↑=태움, default 0.6)"),
    trend_adaptive_exit_chop_factor: Optional[float] = Query(None, ge=1.0, le=3.0, description="chopper factor >1 (preserve↑/slip↓=스캘프, default 1.4)"),
    trend_adaptive_exit_adx_cache_sec: Optional[float] = Query(None, ge=5, le=300, description="코인 ADX 캐시 TTL초 (default 30)"),
    imminent_flip_enabled: Optional[bool] = Query(None, description="freeze 윈도우 안에서도 imminent flip 신호 시 freeze 해제"),
    imminent_flip_ema_gap_pct: Optional[float] = Query(None, ge=0.0, le=10.0, description="BTC EMA20-50 gap 임계 (0.3=기본)"),
    imminent_flip_use_30m: Optional[bool] = Query(None, description="30M 보조 신호 사용"),
    imminent_flip_adx_rise_min: Optional[float] = Query(None, ge=0.0, le=50.0, description="ADX 상승 폭 (2.0=기본)"),
    imminent_flip_gap_lookback: Optional[int] = Query(None, ge=1, le=20, description="gap/ADX 비교 봉 수 (3=기본)"),
    # ── Hard ROE Cap (1건당 최대 손실 ROE 강제 컷, 2026-04-25) ──
    hard_roe_cap_enabled: Optional[bool] = Query(None, description="Hard ROE Cap ON/OFF — 1건당 ROE 임계 도달 시 강제 컷"),
    hard_roe_cap_roe_pct: Optional[float] = Query(None, ge=-99.0, le=0.0, description="Hard ROE Cap 임계 ROE % (음수, 예: -8.0)"),
    # ── Leverage Tier (ATR 기반 차등 레버리지, 2026-04-25) ──
    leverage_tier_enabled: Optional[bool] = Query(None, description="Leverage Tier ON/OFF (ATR 기반 차등)"),
    leverage_tier_atr_low_pct: Optional[float] = Query(None, ge=0.5, le=5.0, description="Low tier ATR 임계 (%) — 미만=low lev"),
    leverage_tier_low: Optional[int] = Query(None, ge=2, le=20, description="Low tier 레버리지 배수"),
    leverage_tier_atr_high_pct: Optional[float] = Query(None, ge=1.0, le=5.0, description="High tier ATR 임계 (%) — 이상=high lev"),
    leverage_tier_high: Optional[int] = Query(None, ge=2, le=20, description="High tier 레버리지 배수"),
    # ── 30M Thesis Invalidation ──
    thesis_invalidation_enabled: Optional[bool] = Query(None, description="30M 구조적 전환 감시 ON/OFF"),
    thesis_invalidation_min_hold_h: Optional[float] = Query(None, ge=0.5, le=4.0, description="최소 보유 시간 (시간)"),
    thesis_invalidation_max_peak_pct: Optional[float] = Query(None, ge=0.1, le=1.0, description="peak 수익 임계값 (%)"),
    # ── Morning Shield / Guard ──
    morning_shield_enabled: Optional[bool] = Query(None, description="Morning Shield (야간 수익 보호) ON/OFF"),
    morning_guard_enabled: Optional[bool] = Query(None, description="Morning Guard (아침 진입 제한) ON/OFF"),
    morning_shield_lock_pct: Optional[float] = Query(None, ge=10, le=90, description="수익 확보율 (%)"),
    morning_guard_conviction_boost: Optional[float] = Query(None, ge=0, le=50, description="[100점] 아침 conviction 상향 (default 15)"),
    morning_guard_end_hour_kst: Optional[float] = Query(None, ge=7.0, le=12.0, description="Guard 종료 시각 (KST)"),
    event_shield_enabled: Optional[bool] = Query(None, description="Event Shield (경제이벤트 방패) ON/OFF"),
    event_shield_times_kst: Optional[str] = Query(None, description="이벤트 시각 CSV ('2026-06-10 21:30, ...') KST"),
    event_shield_window_min: Optional[float] = Query(None, ge=0, le=180, description="이벤트 後 윈도우 (분)"),
    event_shield_lead_min: Optional[float] = Query(None, ge=0, le=120, description="슬리피지 리드 — 이벤트 前은 window+lead분 (군중보다 먼저)"),
    event_shield_lock_pct: Optional[float] = Query(None, ge=10, le=95, description="이벤트 SL 조임 시 이익 보존율 (%)"),
    event_shield_auto_fetch: Optional[bool] = Query(None, description="ForexFactory USD High impact 자동 fetch ON/OFF"),
    auto_tp_enabled: Optional[bool] = Query(None, description="Auto Take-Profit (트레일링 거두기) ON/OFF"),
    auto_tp_usdt: Optional[float] = Query(None, ge=0, description="무장 임계 (순익 이 값 넘으면 그 이익 지킴·USDT)"),
    auto_tp_peak_giveback_pct: Optional[float] = Query(None, ge=0, le=1, description="무장 후 peak 순익에서 이 비율 반납 시 거둠 (0~1)"),
    auto_sl_pct_enabled: Optional[bool] = Query(None, description="Auto Stop-Loss (손실 N% 자동컷) ON/OFF — 평소 OFF"),
    auto_sl_pct: Optional[float] = Query(None, ge=0, le=100, description="컷 손실률 (%)"),
    dual_direction_observe: Optional[bool] = Query(None, description="양방향 평가 Phase 1 관찰 (진입 변경 X · 반대 방향 그림자 채점 기록) ON/OFF"),
    dual_direction_enabled: Optional[bool] = Query(None, description="양방향 평가 Phase 2 — 실제 진입 방향을 높은 쪽으로 결정 (direction_mode=both 일 때) ON/OFF"),
    # ── Erosion Guard ──
    erosion_guard_enabled: Optional[bool] = Query(None, description="Erosion Guard (수익 침식 방지) ON/OFF"),
    erosion_guard_peak_pct: Optional[float] = Query(None, ge=0.1, le=3.0, description="peak 최소 (%)"),
    erosion_guard_ratio: Optional[float] = Query(None, ge=0.1, le=0.9, description="침식 비율 트리거"),
    # ── SL Dodge ──
    sl_dodge_enabled: Optional[bool] = Query(None, description="SL Dodge ON/OFF"),
    sl_dodge_proximity_pct: Optional[float] = Query(None, ge=0.5, le=5.0, description="SL 근접 기준 (%)"),
    sl_dodge_retreat_pct: Optional[float] = Query(None, ge=0.5, le=5.0, description="후퇴 비율 (%)"),
    sl_dodge_max_count: Optional[int] = Query(None, ge=1, le=10, description="최대 dodge 횟수"),
    sl_dodge_max_total_pct: Optional[float] = Query(None, ge=1.0, le=20.0, description="총 dodge 한도 (%)"),
    # ── SL Decay ──
    sl_decay_enabled: Optional[bool] = Query(None, description="SL Decay (시간경과 SL 축소) ON/OFF"),
    sl_decay_2h_ratio: Optional[float] = Query(None, ge=0.1, le=1.0, description="2시간 후 SL 비율"),
    sl_decay_3h_ratio: Optional[float] = Query(None, ge=0.1, le=1.0, description="3시간 후 SL 비율"),
    # ── Fast-Reject ──
    fast_reject_enabled: Optional[bool] = Query(None, description="Fast-Reject (조기 손절) ON/OFF"),
    fast_reject_min_sec: Optional[float] = Query(None, ge=60, le=3600, description="발동 최소 보유 (초)"),
    fast_reject_max_sec: Optional[float] = Query(None, ge=60, le=3600, description="발동 최대 보유 (초)"),
    fast_reject_peak_threshold_pct: Optional[float] = Query(None, ge=0.0, le=2.0, description="peak 미달 임계 (%)"),
    fast_reject_trigger_pnl_pct: Optional[float] = Query(None, ge=-5.0, le=0.0, description="발동 pnl 임계 (%, 음수)"),
    # ── Entry Quality Filter ──
    entry_quality_enabled: Optional[bool] = Query(None, description="Entry Quality Filter (M5 마이크로타이밍) ON/OFF"),
    eq_momentum_enabled: Optional[bool] = Query(None, description="M5 모멘텀 필터 ON/OFF"),
    eq_momentum_count: Optional[int] = Query(None, ge=1, le=5, description="검사 봉 수"),
    eq_momentum_min_agree: Optional[int] = Query(None, ge=1, le=5, description="최소 일치 봉 수"),
    eq_bb_enabled: Optional[bool] = Query(None, description="BB 위치 필터 ON/OFF"),
    eq_bb_upper_pct: Optional[float] = Query(None, ge=50, le=99, description="LONG 차단 BB% 상한"),
    eq_bb_lower_pct: Optional[float] = Query(None, ge=1, le=50, description="SHORT 차단 BB% 하한"),
    eq_nbar_enabled: Optional[bool] = Query(None, description="N봉 추세 필터 ON/OFF"),
    eq_nbar_count: Optional[int] = Query(None, ge=3, le=10, description="추세 검사 봉 수"),
    eq_nbar_min_ratio: Optional[float] = Query(None, ge=0.3, le=1.0, description="HH/LH 최소 비율"),
    # ── Advanced ──
    rr_ratio: Optional[float] = Query(None, ge=1.0, le=10.0, description="Risk-Reward 비율"),
    adaptive_cooldown: Optional[bool] = Query(None, description="적응형 쿨다운 ON/OFF"),
    emergency_tp_tiers: Optional[bool] = Query(None, description="비상 TP 단계 ON/OFF"),
    coin_repeat_window_hours: Optional[float] = Query(None, ge=1, le=72, description="반복 브레이크 윈도우 (시간)"),
    scanner_blacklist: Optional[str] = Query(None, description="스캐너 블랙리스트 (쉼표 구분, 예: CLUSDT,ABCUSDT)"),
    # ── Manual Exit Penalty (수동 탈출 쿨다운) ──
    manual_exit_penalty_enabled: Optional[bool] = Query(None, description="수동 탈출 시 재진입 쿨다운 ON/OFF (OFF=항상 면제)"),
    manual_exit_penalty_hours: Optional[float] = Query(None, ge=0.0, le=24.0, description="손실 탈출 시 쿨다운 시간 (시간)"),
    phase3_context_bonus_enabled: Optional[bool] = Query(None, description="Phase 3 시간대(±4)+코인(+2) 가산점 ON/OFF"),
    # ── [2026-05-19] Advanced 숨겨진 설정 124개 — Query params ──
    # A. Core TF
    primary_tf: Optional[str] = Query(None, description="Primary TF (60=H1, 240=H4)"),
    entry_tf: Optional[str] = Query(None, description="Entry TF (5=M5)"),
    # ★ [2026-05-31 부모 server-b lock_market race 진짜 fix] /config POST 가 lock_market 무시 → 빈 string 보내도 서버 그대로 → 영원히 박힘.
    lock_market: Optional[str] = Query(None, description="Lock to single market (empty=auto-scan / Unlock)"),
    # B. Post-Trade Pause
    post_trade_pause_enabled: Optional[bool] = Query(None, description="거래 후 쿨다운 ON/OFF"),
    post_trade_pause_profit_sec: Optional[float] = Query(None, ge=0, le=14400, description="익절 후 쿨다운 (초)"),
    post_trade_pause_loss_sec: Optional[float] = Query(None, ge=0, le=14400, description="손절 후 쿨다운 (초)"),
    post_trade_pause_fastreject_sec: Optional[float] = Query(None, ge=0, le=14400, description="Fast Reject 후 쿨다운 (초)"),
    post_trade_pause_loss_sliding_enabled: Optional[bool] = Query(None, description="슬라이딩 손실 쿨다운 ON/OFF"),
    post_trade_pause_loss_tier1_pct: Optional[float] = Query(None, ge=0, le=100, description="Tier1 손실 %"),
    post_trade_pause_loss_tier1_sec: Optional[float] = Query(None, ge=0, le=14400, description="Tier1 쿨다운 (초)"),
    post_trade_pause_loss_tier2_pct: Optional[float] = Query(None, ge=0, le=100, description="Tier2 손실 %"),
    post_trade_pause_loss_tier2_sec: Optional[float] = Query(None, ge=0, le=14400, description="Tier2 쿨다운 (초)"),
    post_trade_pause_loss_tier3_pct: Optional[float] = Query(None, ge=0, le=100, description="Tier3 손실 %"),
    post_trade_pause_loss_tier3_sec: Optional[float] = Query(None, ge=0, le=14400, description="Tier3 쿨다운 (초)"),
    post_trade_pause_loss_tier4_pct: Optional[float] = Query(None, ge=0, le=100, description="Tier4 손실 %"),
    post_trade_pause_loss_tier4_sec: Optional[float] = Query(None, ge=0, le=86400, description="Tier4 쿨다운 (초)"),
    post_trade_pause_loss_tier5_sec: Optional[float] = Query(None, ge=0, le=86400, description="Tier5 쿨다운 (초)"),
    # C. Direction Exhaustion
    direction_exhaustion_enabled: Optional[bool] = Query(None, description="방향 소진 차단 ON/OFF"),
    direction_exhaustion_window_sec: Optional[float] = Query(None, ge=60, le=14400, description="감시 윈도우 (초)"),
    direction_exhaustion_profit_count: Optional[int] = Query(None, ge=1, le=10, description="익절 횟수 임계"),
    direction_exhaustion_block_sec: Optional[float] = Query(None, ge=60, le=14400, description="차단 시간 (초)"),
    # D. Coin Reentry Penalty
    coin_reentry_penalty_enabled: Optional[bool] = Query(None, description="재진입 감점 ON/OFF"),
    coin_reentry_penalty_window_sec: Optional[float] = Query(None, ge=60, le=14400, description="감시 윈도우 (초)"),
    coin_reentry_penalty_per_count: Optional[float] = Query(None, ge=0, le=50, description="회당 감점 (100점)"),
    # E. Trailing TP
    trailing_tp_enabled: Optional[bool] = Query(None, description="Trailing TP ON/OFF"),
    trailing_tp_min_progress: Optional[float] = Query(None, ge=0, le=1, description="발동 진행률"),
    trailing_tp_follow_low: Optional[float] = Query(None, ge=0.5, le=1, description="저변동 추적률"),
    trailing_tp_follow_mid: Optional[float] = Query(None, ge=0.5, le=1, description="중변동 추적률"),
    trailing_tp_follow_high: Optional[float] = Query(None, ge=0.5, le=1, description="고변동 추적률"),
    # F. Portfolio SL Rate
    portfolio_sl_rate_enabled: Optional[bool] = Query(None, description="포트폴리오 SL 비율 ON/OFF"),
    portfolio_sl_rate_window_min: Optional[int] = Query(None, ge=1, le=60, description="감시 윈도우 (분)"),
    portfolio_sl_rate_threshold: Optional[int] = Query(None, ge=1, le=20, description="SL 횟수 임계"),
    portfolio_sl_rate_pause_min: Optional[int] = Query(None, ge=1, le=360, description="일시정지 시간 (분)"),
    # G. BTC+B12 Combined Cap
    btc_b12_combined_cap_enabled: Optional[bool] = Query(None, description="BTC+B12 동시 한도 ON/OFF"),
    btc_b12_combined_cap_max: Optional[int] = Query(None, ge=1, le=10, description="합산 최대 슬롯"),
    # H. Override Slot
    override_min_adx: Optional[float] = Query(None, ge=0, le=100, description="Override 최소 ADX"),
    override_min_mtf_align: Optional[int] = Query(None, ge=0, le=6, description="Override 최소 MTF align"),
    override_min_b12_n: Optional[int] = Query(None, ge=0, le=20, description="Override 최소 B12 N"),
    override_require_btc_trend_match: Optional[bool] = Query(None, description="Override BTC 트렌드 일치 요구"),
    override_max_extra_slots: Optional[int] = Query(None, ge=0, le=10, description="Override 최대 추가 슬롯"),
    override_breakeven_trigger_pct: Optional[float] = Query(None, ge=0, le=5, description="Override BE 트리거 (%)"),
    # J. Pair Block
    pair_block_enabled: Optional[bool] = Query(None, description="Pair Block ON/OFF"),
    pair_block_mode: Optional[str] = Query(None, description="Pair Block 모드 (aggressive/conservative)"),
    pair_block_same_limit: Optional[int] = Query(None, ge=1, le=10, description="같은 방향 최대 페어"),
    # K. Coin Profit Lock-in
    coin_profit_lockin_enabled: Optional[bool] = Query(None, description="Profit Lock-in ON/OFF"),
    coin_profit_lockin_window_hours: Optional[float] = Query(None, ge=0.5, le=48, description="보호 시간 (h)"),
    coin_profit_lockin_min_realized: Optional[float] = Query(None, ge=0, le=1000, description="발동 최소 실현 수익 ($)"),
    coin_profit_lockin_protect_ratio: Optional[float] = Query(None, ge=0, le=1, description="보호 비율"),
    coin_profit_lockin_require_be: Optional[bool] = Query(None, description="BE 도달 후 발동"),
    # L. PA Weight
    pa_weight_enabled: Optional[bool] = Query(None, description="PA Weight ON/OFF"),
    pa_weight_pin_bar: Optional[int] = Query(None, ge=0, le=10, description="PIN_BAR 가중치"),
    pa_weight_engulfing: Optional[int] = Query(None, ge=0, le=10, description="ENGULFING 가중치"),
    pa_weight_star_v1: Optional[int] = Query(None, ge=0, le=10, description="STAR_V1 가중치"),
    pa_weight_star_v2: Optional[int] = Query(None, ge=0, le=10, description="STAR_V2 가중치"),
    pa_weight_squeeze_break: Optional[int] = Query(None, ge=0, le=10, description="SQUEEZE_BREAK 가중치"),
    pa_weight_bos: Optional[int] = Query(None, ge=0, le=10, description="BOS 가중치"),
    pa_weight_zone_bonus: Optional[int] = Query(None, ge=0, le=10, description="Zone 보너스"),
    pa_zone_proximity_atr: Optional[float] = Query(None, ge=0, le=5, description="Zone ATR 배수"),
    pa_location_penalty_far: Optional[float] = Query(None, ge=0, le=5, description="멀리 페널티"),
    # O. Session Profile times
    sess_quiet_start_kst: Optional[float] = Query(None, ge=0, le=24, description="quiet 시작 KST (h)"),
    sess_quiet_end_kst: Optional[float] = Query(None, ge=0, le=24, description="quiet 종료 KST (h)"),
    sess_active_start_kst: Optional[float] = Query(None, ge=0, le=24, description="active 시작 KST (h)"),
    sess_active_end_kst: Optional[float] = Query(None, ge=0, le=24, description="active 종료 KST (h)"),
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
    session_profile_enabled: Optional[bool] = Query(None, description="Session Profile (KST 시간대 conviction ±) ON/OFF"),
    direction_memory_enabled: Optional[bool] = Query(None, description="Direction Memory (코인+방향 연패 soft penalty) ON/OFF"),
    dm_streak_block_enabled: Optional[bool] = Query(None, description="Direction Memory hard block (N연패 시 진입 차단) ON/OFF"),
    dm_streak_block: Optional[int] = Query(None, ge=2, le=20, description="Hard block 발동 연패 횟수 (기본 4)"),
    dm_streak_block_hours: Optional[float] = Query(None, ge=0.1, le=168.0, description="DM Streak Hard block 차단 지속 시간(h)"),
    dm_streak_block_opposite: Optional[bool] = Query(None, description="DM Streak: 반대 방향도 차단 (False=FLIP 허용)"),
    # ★ Phase F (2026-04-20): Profit Exit Block 3-tuple 설정
    profit_exit_block_enabled: Optional[bool] = Query(None, description="B10 Profit Exit Block ON/OFF"),
    profit_exit_block_min_consecutive: Optional[int] = Query(None, ge=2, le=10, description="발동 연승 횟수 (기본 3)"),
    profit_exit_block_hours: Optional[float] = Query(None, ge=1, le=72, description="차단 지속 시간(h)"),
    profit_exit_block_block_opposite: Optional[bool] = Query(None, description="반대 방향도 차단 (기본 False=FLIP 허용)"),
    # ── ★ [2026-05-18 부모 요청] Consecutive Loss Pause (옛 누락, router/UI 추가) ──
    consecutive_loss_pause_enabled: Optional[bool] = Query(None, description="N연패 후 자동 정지 (큰 누적손실 회피)"),
    consecutive_loss_pause_count: Optional[int] = Query(None, ge=2, le=20, description="발동 연패 횟수 (default 3, 검증 모드 권장 10)"),
    consecutive_loss_pause_min: Optional[int] = Query(None, ge=1, le=1440, description="정지 시간 분 (default 60, 검증 모드 권장 10)"),
    # ── ★ [2026-06-04 부모] 방향별 레짐 윈도우 실패 차단 ──
    regime_direction_fail_enabled: Optional[bool] = Query(None, description="방향별 레짐 윈도우 실패 차단 ON/OFF — N시간 내 LONG N회 실패 → LONG만 차단, SHORT는 허용"),
    regime_direction_fail_window_hours: Optional[float] = Query(None, ge=1, le=24, description="레짐 윈도우 (시간, 기본 4.0=H4 한 봉)"),
    regime_direction_fail_max: Optional[int] = Query(None, ge=1, le=10, description="허용 실패 횟수 (기본 3, 초과 시 해당 방향 차단)"),
    btc_regime_enabled: Optional[bool] = Query(None, description="BTC Regime (BTC primary_tf(H1) EMA 레짐 인식 - 역방향 페널티) ON/OFF"),
    btc_regime_bear_long_delta: Optional[float] = Query(None, ge=-100, le=0, description="[100점 ×10] BEAR 시장 LONG 진입 페널티 (default -20). 입력 시 bull_short_delta 도 동일 적용."),
    market_bias_enabled: Optional[bool] = Query(None, description="Market Bias (다중 코인 EXIT 쏠림 인식 - 역행 페널티) ON/OFF"),
    mb_against_delta: Optional[float] = Query(None, ge=-100, le=0, description="[100점 ×10] 쏠림 역행 페널티 (default -10)"),
    sess_quiet_delta: Optional[float] = Query(None, ge=-100, le=0, description="[100점 ×10] KST 01~06h quiet 시간 페널티 (default -10)"),
    sess_active_delta: Optional[float] = Query(None, ge=0, le=100, description="[100점 ×10] KST 21~24h active 시간 보너스 (default +10)"),
    dm_loss_count_delta: Optional[float] = Query(None, ge=-100, le=0, description="[100점 ×10] 같은 코인×방향 연패 페널티 (default -20)"),
    # ── B11 Regime Direction Lock (2026-04-19 하드 차단, 3축 토글) ──
    regime_direction_lock_enabled: Optional[bool] = Query(None, description="B11 Regime Direction Lock 마스터 (BULL→LONG only / BEAR→SHORT only / NEUTRAL→REST)"),
    regime_lock_use_slope: Optional[bool] = Query(None, description="B11 Slope 축 — EMA20 기울기 ≥0.3% 엄격 판정 ON/OFF"),
    regime_lock_use_distance: Optional[bool] = Query(None, description="B11 Distance 축 — EMA50 대비 거리 ≥1.0% 엄격 판정 ON/OFF"),
    regime_lock_use_cross: Optional[bool] = Query(None, description="B11 Direction 축 — EMA20 vs EMA50 방향 판정 ON/OFF (코어)"),
    # ── B12 Scanner Breadth Lock (2026-04-23 부모 직접 요청, B11과 mutually exclusive) ──
    regime_lock_mode: Optional[str] = Query(None, description="Regime Lock Mode: B11 (BTC EMA) / B12 (Scanner 합창) / OFF (둘 다 해제)"),
    b12_threshold_n: Optional[int] = Query(None, ge=1, le=20, description="B12 N: 같은 방향 가리키는 최소 마켓 수 (default 6 = 75% of 8)"),
    b12_window_sec: Optional[float] = Query(None, ge=60.0, le=3600.0, description="B12 투표 집계 윈도우 (sec, default 1200=20분, 데이터 분석 기반)"),
    # ── 방향별 슬롯 상한 (2026-04-20 형 지시, -1=Auto, 0=차단, N=명시) ──
    max_long_positions: Optional[int] = Query(None, ge=-1, le=50, description="LONG 슬롯 상한 (-1=Auto=max_same_direction, 0=완전 차단, N=명시)"),
    max_short_positions: Optional[int] = Query(None, ge=-1, le=50, description="SHORT 슬롯 상한 (-1=Auto=max_same_direction, 0=완전 차단, N=명시)"),
    auto_first_dir_lock: Optional[bool] = Query(None, description="[2026-04-26] Auto 모드 첫 발 방향 잠금 (true=초단타, false=롱홀드 양방향 자유)"),
    # 똑똑하게 #2,#4,#5,#6,#7 (2026-04-26 부모님 1단계)
    regime_reversal_pause_enabled: Optional[bool] = Query(None),
    regime_reversal_ema_gap_threshold_pct: Optional[float] = Query(None, ge=0.01, le=5.0),
    regime_reversal_adx_threshold: Optional[float] = Query(None, ge=5.0, le=50.0),
    regime_reversal_pause_min: Optional[float] = Query(None, ge=1.0, le=240.0),
    conv_sizing_enabled: Optional[bool] = Query(None),
    conv_sizing_low_threshold: Optional[float] = Query(None, ge=0, le=100),
    conv_sizing_high_threshold: Optional[float] = Query(None, ge=0, le=100),
    conv_risk_scale_enabled: Optional[bool] = Query(None, description="[진입점수=확신] guard점수 기준 risk 역U자 스케일 ON/OFF (default OFF)"),
    conv_risk_peak_conv: Optional[float] = Query(None, ge=0, le=200, description="[진입점수=확신] sweet spot 시작/정점 conviction (default 65)"),
    conv_risk_peak_mult: Optional[float] = Query(None, ge=0.1, le=3, description="[진입점수=확신] sweet spot risk 배수 (default 1.5)"),
    conv_risk_chop_conv: Optional[float] = Query(None, ge=0, le=200, description="[진입점수=확신] 끝물 라인 (default 80)"),
    conv_risk_chop_mult: Optional[float] = Query(None, ge=0.1, le=2, description="[진입점수=확신] 끝물 risk 컷 배수 (default 0.6)"),
    conv_risk_floor_mult: Optional[float] = Query(None, ge=0.1, le=2, description="[진입점수=확신] 임계 미만 배수 (default 0.5)"),
    conv_risk_max_mult: Optional[float] = Query(None, ge=0.5, le=5, description="[진입점수=확신] factor 안전 상한 (default 2.0)"),
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
    multi_be_lock_fee_cushion_pct: Optional[float] = Query(None, ge=0.0, le=2.0, description="stage1 fee cushion (기본 0.05, BE 후 건당 소액 fee 손실 방지)"),
    # ── ★ [2026-06-04 부모] Smart BE Lock ──
    be_lock_smart_rsi_check: Optional[bool] = Query(None, description="① RSI 이윤 방향 체크 — 달리는 중이면 BE 보류"),
    be_lock_smart_candle_check: Optional[bool] = Query(None, description="② 직전 N봉 연속 이윤 방향 체크 — 가속 중이면 BE 보류"),
    be_lock_smart_rsi_long_min: Optional[float] = Query(None, ge=40, le=80, description="LONG: RSI ≥ 이 값 = 달리는 중 (기본 55)"),
    be_lock_smart_rsi_short_max: Optional[float] = Query(None, ge=20, le=60, description="SHORT: RSI ≤ 이 값 = 달리는 중 (기본 45)"),
    be_lock_smart_candle_count: Optional[int] = Query(None, ge=2, le=10, description="직전 N봉(5M) 연속 이윤 방향 (기본 3)"),
    parent_roe_guard_enabled: Optional[bool] = Query(None),
    parent_max_roe_loss_pct: Optional[float] = Query(None, ge=10.0, le=99.0),
    # ── Phase J v2 (2026-04-21): ADX 하락 중 방향 무관 skip — 시장 식어감 감지 ──
    adx_slope_check_enabled: Optional[bool] = Query(None, description="Phase J v2: ADX 하락 중 방향 무관 진입 skip ON/OFF"),
    adx_slope_lookback_bars: Optional[int] = Query(None, ge=1, le=10, description="Phase J v2: 몇 primary_tf(H1) 봉 전 대비 비교 (기본 3=3h)"),
    adx_slope_decline_threshold_pct: Optional[float] = Query(None, ge=0.5, le=30.0, description="Phase J v2: N% 이상 하락 시 skip (기본 2.0)"),
    # ── Phase K (2026-04-21): Regime Transition Preemptive Entry ──
    # ⚠️ J v2 와 상호 배제 — K=True 면 J v2 자동 무시. 같은 ADX 하락 신호에 K=flip 우선.
    # paper_mode=True 면 진입 없이 phase_k_paper_log.jsonl 만 기록.
    regime_transition_enabled: Optional[bool] = Query(None, description="Phase K: Regime Transition Preemptive Entry ON/OFF (⚠️ J v2 자동 OFF)"),
    regime_transition_paper_mode: Optional[bool] = Query(None, description="Phase K: paper mode (True=진입없이 JSONL기록만, False=실진입)"),
    regime_transition_size_mult: Optional[float] = Query(None, ge=0.1, le=0.5, description="Phase K: size multiplier (0.3 floor → 0.5 CAP — 검수 Q4 확정)"),
    regime_transition_tp_mult: Optional[float] = Query(None, ge=0.3, le=1.5, description="Phase K: TP multiplier (기본 0.7 초단기)"),
    regime_transition_sl_mult: Optional[float] = Query(None, ge=0.3, le=1.5, description="Phase K: SL multiplier (기본 0.8 타이트)"),
    regime_transition_adx_decline_ratio: Optional[float] = Query(None, ge=0.80, le=0.99, description="Phase K: adx_now < adx_past × ratio 조건 (기본 0.95)"),
    regime_transition_ema_gap_threshold_pct: Optional[float] = Query(None, ge=0.1, le=2.0, description="Phase K: BTC |EMA20-50|/price 임계 (기본 0.3%)"),
    regime_transition_min_conviction: Optional[float] = Query(None, ge=0, le=100, description="[100점] Phase K 최소 conviction (default 75)"),
    regime_transition_last_change_age_min: Optional[float] = Query(None, ge=30.0, le=1440.0, description="Phase K: regime 전환 후 최소 age (분, 기본 180)"),
    regime_transition_daily_fail_limit: Optional[int] = Query(None, ge=1, le=20, description="Phase K: 일일 실패 한도 (기본 3, v2 자동 off 로직)"),
    regime_transition_weekly_fail_limit: Optional[int] = Query(None, ge=1, le=50, description="Phase K: 주간 실패 한도 (기본 5)"),
    regime_transition_min_mtf_align: Optional[int] = Query(None, ge=1, le=5, description="Phase K: MTF 정렬 최소 수 (v1 미사용)"),
    # ── Phase L (2026-04-22): S3 Fee-Aware net_ev Gate ──
    # 형 letter #11 검수 기준 10항목 + 엣지 7건. paper_mode 1주 → 7 조건 통과 → live.
    s3_gate_enabled: Optional[bool] = Query(None, description="Phase L: S3 Fee-Aware Gate ON/OFF (default OFF)"),
    s3_gate_paper_mode: Optional[bool] = Query(None, description="Phase L: paper mode (True=skip 안 함, 가상 카운터)"),
    s3_gate_min_net_ev_usdt: Optional[float] = Query(None, ge=-10.0, le=100.0, description="Phase L: net_ev 임계 ($), 기본 0 = 손익분기"),
    s3_gate_fee_multiplier: Optional[float] = Query(None, ge=1.0, le=5.0, description="Phase L: 수수료 안전 마진 배수 (기본 2 = 왕복)"),
    s3_gate_slippage_bps: Optional[float] = Query(None, ge=0.0, le=50.0, description="Phase L: 슬리피지 bp (기본 5 = 0.05%)"),
    s3_gate_link_multiplier: Optional[float] = Query(None, ge=1.0, le=3.0, description="Phase L: LINK 도박기질 가드 배수 (기본 1.3)"),
    # 🪙 Orderbook 깊이 사이즈 적응 (2026-06-09 부모 "잔돈 없는 환전소")
    orderbook_depth_sizing_enabled: Optional[bool] = Query(None, description="🪙 진입 사이즈를 호가 수용량에 맞춤 — 미체결 방지 (default OFF)"),
    orderbook_depth_max_slippage_pct: Optional[float] = Query(None, ge=0.05, le=2.0, description="이 % 이내 호가까지 체결가능으로 집계 (기본 0.3)"),
    orderbook_depth_min_fill_ratio: Optional[float] = Query(None, ge=0.1, le=1.0, description="수용량/의도 < 이 비율이면 skip (기본 0.5)"),
    # ── Phase L.1 ─ Fast-Reject 강화 (Harpoon 0-6 → FOCUS 이식) ──
    fast_reject_v2_enabled: Optional[bool] = Query(None, description="Phase L.1: 30초 peak 0% 즉시 컷 (default OFF)"),
    fast_reject_v2_max_sec: Optional[float] = Query(None, ge=10.0, le=300.0, description="Phase L.1: 검사 최대 초"),
    fast_reject_v2_peak_threshold_pct: Optional[float] = Query(None, ge=0.0, le=1.0, description="Phase L.1: peak 임계 (%)"),
    fast_reject_v2_pnl_pct: Optional[float] = Query(None, ge=-2.0, le=0.0, description="Phase L.1: pnl 임계 (%, 음수)"),
    # ── Phase L.2 ─ 재진입 30분 cooldown (Harpoon 0-7 → FOCUS 이식) ──
    reentry_cooldown_v2_enabled: Optional[bool] = Query(None, description="Phase L.2: SL 후 30분 동일 코인+방향 차단 (default OFF)"),
    reentry_cooldown_v2_min: Optional[float] = Query(None, ge=5.0, le=240.0, description="Phase L.2: 차단 분"),
    # ── Phase L.3 ─ PA 2건 합의 (S3 Gate 품질 게이트) ──
    pa_double_confirm_enabled: Optional[bool] = Query(None, description="Phase L.3: PA 2건 합의 시 S3 Gate net_ev × 1.10 (default OFF)"),
    pa_double_confirm_window_sec: Optional[float] = Query(None, ge=30.0, le=300.0, description="Phase L.3: 합의 윈도우 (초)"),
    # ── ★★★ [2026-05-27 부모 정신] Phase 6 — H4/H1 PA + 점수 통합 + 5M 긴급탈출 ──
    # α. Phase 6 점수 통합 (Master)
    guard_score_mode_enabled: Optional[bool] = Query(None, description="Phase 6 점수 통합 마스터 — 가드 차단 → conv 가산/감점 (운영자 5-27)"),
    guard_score_mode_auto_paper: Optional[bool] = Query(None, description="auto_paper: paper 자동 ON / LIVE 자동 OFF (사무실 보호)"),
    guard_score_threshold: Optional[float] = Query(None, ge=0, le=200, description="Phase 6 진입 임계 (default 80)"),
    # ★ [2026-06-12] 추세정렬 multicollinearity 캡
    regime_align_cap_enabled: Optional[bool] = Query(None, description="추세정렬(Frame+Trend+AltBTC) 합산 캡 — 역행 다리 핸디캡 축소(SHORT 공평) ON/OFF"),
    regime_align_cap: Optional[float] = Query(None, ge=0, le=60, description="추세정렬 합산 클램프 ±값 (default 15)"),
    combo_f_dedupe_enabled: Optional[bool] = Query(None, description="combo_f 방향 이중계산(F1 MTF·F2 M5) 제거 — SHORT 적자 최대원인 fix ON/OFF"),
    guard_dir_dedupe_enabled: Optional[bool] = Query(None, description="guard 방향 이중계산(Frame/Trend/AltBTC/BTC정렬=conviction과 중복) 제거 ON/OFF"),
    # β. PA Completion (Sig + ไส้หลัง) ⭐
    pa_completion_enabled: Optional[bool] = Query(None, description="PA Completion — Pat 1/2/3 외 진입 불가 ⭐ 운영자 핵심"),
    pa_completion_auto_paper: Optional[bool] = Query(None, description="auto_paper: paper ON / LIVE OFF"),
    pa_completion_huikkang_min_ratio: Optional[float] = Query(None, ge=0.5, le=5, description="ไส้หลัง body 최소 배수 (default 1.5)"),
    pa_completion_lookback_bars: Optional[int] = Query(None, ge=2, le=10, description="lookback bars (default 3)"),
    pa_completion_sig_max_ratio: Optional[float] = Query(None, ge=0.3, le=3, description="Sig body 최대 비율 (default 1.0)"),
    # γ. H4 Pulse Only
    h4_pulse_only_enabled: Optional[bool] = Query(None, description="H4 Pulse Only — H4 마감 후 N분만 진입"),
    h4_pulse_auto_paper: Optional[bool] = Query(None, description="auto_paper: paper ON / LIVE OFF"),
    h4_pulse_window_min: Optional[int] = Query(None, ge=5, le=240, description="H4 펄스 창 분 (default 60)"),
    # ── [패치 v2-A] Pre-Close 선행 진입 ──
    preclose_entry_enabled: Optional[bool] = Query(None, description="[패치v2-A] H4 마감 전 형태완성 선진입 ON/OFF (default OFF)"),
    preclose_min_elapsed_pct: Optional[float] = Query(None, ge=50, le=100, description="[패치v2-A] H4 진행봉 경과율 임계 (default 88)"),
    preclose_size_ratio: Optional[float] = Query(None, ge=0.05, le=1.0, description="[패치v2-A] 선행 진입 사이즈 비율 (default 0.5)"),
    preclose_wick_ratio_min: Optional[float] = Query(None, ge=0.5, le=5.0, description="[패치v2-A] 핀바 꼬리/몸통 비율 (default 1.5)"),
    preclose_body_dir_required: Optional[bool] = Query(None, description="[패치v2-A] 몸통방향+종가위치 조건 사용 (default True)"),
    preclose_max_per_day: Optional[int] = Query(None, ge=0, le=50, description="[패치v2-A] 일일 선행 진입 상한 (default 5)"),
    preclose_min_conviction: Optional[float] = Query(None, ge=0, le=200, description="[패치v2-A] 선행 자격 base conviction 하한 (default 50)"),
    preclose_topup_enabled: Optional[bool] = Query(None, description="[패치v2-A2] 마감 확인 증액(2차 진입) ON/OFF (default OFF)"),
    preclose_topup_min_pnl_pct: Optional[float] = Query(None, ge=-5, le=10, description="[패치v2-A2] 증액 확인 pnl 하한 % (default 0)"),
    preclose_topup_max_chase_pct: Optional[float] = Query(None, ge=0, le=10, description="[패치v2-A2] 과다추격 캡 % (default 1)"),
    preclose_topup_require_candle_dir: Optional[bool] = Query(None, description="[패치v2-A2] 직전 마감 H4봉 방향 일치 요구 (default True)"),
    preclose_topup_grace_min: Optional[float] = Query(None, ge=5, le=240, description="[패치v2-A2] 마감 후 증액 허용 창(분) (default 60)"),
    # δ. H1 PA Pulse
    h1_pa_pulse_enabled: Optional[bool] = Query(None, description="H1 PA Pulse — H4 외 H1 PA 도 진입 (운영자 5-27 ②)"),
    h1_pa_pulse_auto_paper: Optional[bool] = Query(None, description="auto_paper: paper ON / LIVE OFF"),
    h1_pa_pulse_window_min: Optional[int] = Query(None, ge=5, le=60, description="H1 펄스 창 분 (default 15)"),
    h1_pa_pulse_lookback_bars: Optional[int] = Query(None, ge=1, le=5, description="lookback bars (default 2)"),
    h1_pa_pulse_min_confidence: Optional[float] = Query(None, ge=0, le=1, description="min confidence (default 0.5)"),
    h1_pa_pulse_require_day_dir: Optional[bool] = Query(None, description="day_direction 정렬 강제 (default true)"),
    # ε. Anchor Fast-Track
    anchor_fasttrack_enabled: Optional[bool] = Query(None, description="Anchor 근처 즉시 진입 — microtiming 5M 우회"),
    anchor_fasttrack_auto_paper: Optional[bool] = Query(None, description="auto_paper: paper ON / LIVE OFF"),
    anchor_fasttrack_max_proximity: Optional[float] = Query(None, ge=0.05, le=1.0, description="max proximity (default 0.33)"),
    # ζ. Day Box Guard
    day_box_guard_enabled: Optional[bool] = Query(None, description="Day Box — D1 9시 박스 핑퐁 상하한선 (운영자 5-27 ③)"),
    day_box_guard_auto_paper: Optional[bool] = Query(None, description="auto_paper: paper ON / LIVE OFF"),
    day_box_window_hours: Optional[float] = Query(None, ge=1, le=12, description="박스 형성 시간 (default 4.0)"),
    day_box_lock_min_hours: Optional[float] = Query(None, ge=0.5, le=12, description="lock 판정 가능 최소 시간 (default 3.5)"),
    day_box_max_atr_ratio: Optional[float] = Query(None, ge=0.1, le=3, description="최대 ATR 비율 (default 0.8)"),
    day_box_min_touches: Optional[int] = Query(None, ge=1, le=10, description="양극점 최소 터치 (default 2)"),
    day_box_edge_pct: Optional[float] = Query(None, ge=0.01, le=0.3, description="Edge 구간 (0~1, default 0.05)"),
    day_box_breakout_pct: Optional[float] = Query(None, ge=0.01, le=2, description="돌파 판정 % (default 0.10)"),
    # η. TF Round TP/SL
    tf_round_tpsl_enabled: Optional[bool] = Query(None, description="TF-Round TP/SL — H4/H1 PA anchor 라운드사다리"),
    tf_round_tpsl_auto_paper: Optional[bool] = Query(None, description="auto_paper: paper ON / LIVE OFF"),
    tf_round_anchor_tf: Optional[str] = Query(None, description="anchor TF (60=H1, 240=H4)"),
    tf_round_atr_period: Optional[int] = Query(None, ge=3, le=50, description="ATR period (default 14)"),
    tf_round_tp_atr_mult: Optional[float] = Query(None, ge=0.3, le=5, description="TP1 ATR 배수 (default 1.0)"),
    tf_round_tp2_atr_mult: Optional[float] = Query(None, ge=0.5, le=10, description="TP2 ATR 배수 (default 2.0)"),
    tf_round_sl_ratio: Optional[float] = Query(None, ge=0.1, le=1, description="SL 비율 TP1 대비 (default 0.3333)"),
    tf_round_anchor_offset: Optional[int] = Query(None, ge=0, le=5, description="anchor offset (default 0)"),
    tf_round_hold_enabled: Optional[bool] = Query(None, description="견딤 모드 (단기컷 OFF)"),
    # θ. Frame Guard Option B
    frame_guard_option_b_enabled: Optional[bool] = Query(None, description="Frame Guard Option B — 90s silent skip"),
    frame_guard_option_b_auto_paper: Optional[bool] = Query(None, description="auto_paper: paper ON / LIVE OFF"),
    # ι. 5M Emergency Exit ⭐
    exit_5m_emergency_enabled: Optional[bool] = Query(None, description="5M 긴급 탈출 — RSI/MACD/BB '여기서 그만!' ⭐ 운영자 핵심"),
    exit_5m_emergency_auto_paper: Optional[bool] = Query(None, description="auto_paper: paper ON / LIVE OFF"),
    exit_5m_rsi_overbought: Optional[float] = Query(None, ge=50, le=100, description="LONG 청산 RSI 임계 (default 70)"),
    exit_5m_rsi_oversold: Optional[float] = Query(None, ge=0, le=50, description="SHORT 청산 RSI 임계 (default 30)"),
    exit_5m_bb_top_pct: Optional[float] = Query(None, ge=50, le=100, description="LONG 청산 BB position 임계 (default 90)"),
    exit_5m_bb_bottom_pct: Optional[float] = Query(None, ge=0, le=50, description="SHORT 청산 BB position 임계 (default 10)"),
    exit_5m_min_score: Optional[int] = Query(None, ge=1, le=3, description="3종 중 N 충족 (default 2)"),
    # κ. Guard Score Weights
    guard_score_pa_completion_ok: Optional[float] = Query(None, ge=0, le=100, description="PA 완성 가산점 (default 30)"),
    guard_score_pa_completion_none: Optional[float] = Query(None, ge=-100, le=0, description="PA 없음 감점 (default -25)"),
    guard_score_d1_pa_ok: Optional[float] = Query(None, ge=0, le=100, description="D1 PA OK 가산점 (default 25)"),
    guard_score_d1_pa_none: Optional[float] = Query(None, ge=-100, le=0, description="D1 PA 없음 감점 (default -15)"),
    guard_score_btc_aligned: Optional[float] = Query(None, ge=0, le=100, description="BTC 정렬 가산점 (default 15)"),
    guard_score_btc_opposite: Optional[float] = Query(None, ge=-100, le=0, description="BTC 역행 감점 (default -15)"),
    guard_score_adx_strong: Optional[float] = Query(None, ge=0, le=100, description="ADX 강 가산점 (default 10)"),
    guard_score_adx_weak: Optional[float] = Query(None, ge=-50, le=0, description="ADX 약 감점 (default -5)"),
    guard_score_adx_strong_requires_trend: Optional[bool] = Query(None, description="ADX 강가점을 구조 SIDEWAYS면 면제(점수↔차트 정합). default OFF=라이브 0변화"),
    naked_sl_guard_enabled: Optional[bool] = Query(None, description="서버SL 미확정+SL근접 시 즉시 시장가 청산(무사통과 청산 방지). 안전망, 기본 ON"),
    naked_sl_guard_buffer_pct: Optional[float] = Query(None, ge=0, le=5, description="naked SL 근접 버퍼 %%(grace 후 선제 컷)"),
    server_sl_verify_enabled: Optional[bool] = Query(None, description="SYNC마다 거래소 실제 stopLoss 읽어 대조→없/불일치 재배치. 안전망, 기본 ON"),
    guard_score_vol_big_align: Optional[float] = Query(None, ge=0, le=50, description="vol big 가산점 (default 10)"),
    guard_score_trend_high_conf: Optional[float] = Query(None, ge=0, le=50, description="Trend 고신뢰 가산점 (default 10)"),
    guard_score_trend_low_conf: Optional[float] = Query(None, ge=-50, le=0, description="Trend 저신뢰 감점 (default -5)"),
    guard_score_rsi_extreme: Optional[float] = Query(None, ge=0, le=50, description="RSI 극단 가산점 (default 10)"),
    guard_score_h4_pulse_in: Optional[float] = Query(None, ge=0, le=100, description="H4 펄스 안 가산점 (default 20)"),
    guard_score_h4_pulse_out: Optional[float] = Query(None, ge=-100, le=0, description="H4 펄스 밖 감점 (default -10)"),
    guard_score_h1_pa_in: Optional[float] = Query(None, ge=0, le=100, description="H1 PA 통과 가산점 (default 15)"),
    guard_score_h1_pa_out: Optional[float] = Query(None, ge=-50, le=0, description="H1 PA 미통과 감점 (default -5)"),
    guard_score_frame_aligned: Optional[float] = Query(None, ge=0, le=100, description="Frame 정렬 가산점 (default 15)"),
    guard_score_frame_neutral: Optional[float] = Query(None, ge=-20, le=50, description="Frame 중립 (default 5)"),
    guard_score_frame_opposite: Optional[float] = Query(None, ge=-100, le=0, description="Frame 반대 감점 (default -20)"),
    guard_score_anchor_close: Optional[float] = Query(None, ge=0, le=100, description="Anchor 가까움 가산점 (default 20)"),
    guard_score_anchor_far: Optional[float] = Query(None, ge=-100, le=0, description="Anchor 멀음 감점 (default -10)"),
    guard_score_day_box_edge: Optional[float] = Query(None, ge=0, le=50, description="Day Box edge 가산점 (default 10)"),
    guard_score_day_box_inside: Optional[float] = Query(None, ge=-100, le=0, description="Day Box 안 감점 (default -15)"),
    guard_score_microtiming_ok: Optional[float] = Query(None, ge=0, le=50, description="microtiming OK 가산점 (default 10)"),
    guard_score_microtiming_no: Optional[float] = Query(None, ge=-50, le=0, description="microtiming X 감점 (default -5)"),
    guard_score_raw_body_align: Optional[float] = Query(None, ge=0, le=50, description="raw_body 정렬 가산점 (default 5)"),
    guard_score_raw_body_against: Optional[float] = Query(None, ge=-100, le=0, description="raw_body 반대 감점 (default -15)"),
    guard_score_momentum_deriv_align: Optional[float] = Query(None, ge=0, le=50, description="momentum 일치 가산점 (default 5)"),
    guard_score_momentum_deriv_against: Optional[float] = Query(None, ge=-50, le=0, description="momentum 반대 감점 (default -10)"),
    # ── ★ [2026-06-03 부모] D1 추세 가중 + 갭 체크 게이트 ──
    d1_trend_weight: Optional[float] = Query(None, ge=0, le=3, description="D1(일봉) 추세 가중 (default 1.0, ×6=최대)"),
    cr_speed_sign_guard_enabled: Optional[bool] = Query(None, description="Fix A — cr 방향(개수)이 UP인데 실제 변화율 음수면 중립(가짜 UP 가점 차단). BEAT사고. default OFF"),
    cr_blowoff_extreme_guard_enabled: Optional[bool] = Query(None, description="Fix B — 극단 폭등/폭락(blowoff)=끝물 → cr 방향 중립. D1 +103% 잔상 차단. default OFF"),
    cr_blowoff_extreme_ratio: Optional[float] = Query(None, ge=1, le=20, description="blowoff 임계 speed/ATR ratio (default 4.0, 낮출수록 자주 끝물 판정)"),
    cr_trend_agree_guard_enabled: Optional[bool] = Query(None, description="Fix C — 5캔들 방향이 더 긴 추세(lookback)와 반대면 중립. 잔물결을 추세로 착각 차단. default OFF"),
    cr_trend_agree_lookback: Optional[int] = Query(None, ge=6, le=120, description="Fix C 큰 추세 판정 캔들수 (default 20)"),
    gap_check_enabled: Optional[bool] = Query(None, description="갭 체크 게이트 — 진입 전 TF×N봉 천장/바닥까지 거리 확인 ON/OFF"),
    gap_check_tf: Optional[str] = Query(None, description="갭 체크 TF (5 / 15 / 30 / 60)"),
    gap_check_lookback_bars: Optional[int] = Query(None, ge=6, le=48, description="갭 체크 lookback 봉수 (default 12, 12×15M=3h)"),
    gap_check_min_pct: Optional[float] = Query(None, ge=0, le=5, description="최소 갭 % (0=OFF, 0.3 권장)"),
    gap_check_atr_adaptive_enabled: Optional[bool] = Query(None, description="갭 ATR 적응 — 등락폭 큰 코인은 필요 갭↑ (꼭대기 추격 차단, 2026-06-07)"),
    gap_check_atr_mult: Optional[float] = Query(None, ge=0, le=3, description="필요 갭 = ATR% × 이값 (default 0.7, 클수록 더 아래에서만 진입)"),
    gap_check_atr_cap_pct: Optional[float] = Query(None, ge=0.1, le=5, description="ATR 적응 필요갭 상한 % (default 1.5)"),
    gap_proximity_exit_enabled: Optional[bool] = Query(None, description="갭 접근 청산 — 천장/바닥 근접 선제 탈출 ON/OFF"),
    gap_proximity_exit_tf: Optional[str] = Query(None, description="갭 접근 청산 TF (5 / 15 / 30 / 60)"),
    gap_proximity_exit_pct: Optional[float] = Query(None, ge=0.1, le=2, description="접근 임계 % (이 이내 접근 시 청산, default 0.2)"),
    # ── ★ [2026-06-15 해결안 B·C] 관측 토글 (진입 로직 불침, default OFF) ──
    gate_ledger_enabled: Optional[bool] = Query(None, description="B: 게이트 통과/거절 집계('왜 침묵했나'). 관측만, 진입 무관"),
    dual_observe_auto_off_weak: Optional[bool] = Query(None, description="C: 약서버(RAM≤임계)에서 dual observe 자동 OFF (부하↓). 진입 불변, 강서버 무영향"),
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
        # ★ Per-Coin Size Cap (2026-05-08 부모님 결정)
        "per_coin_size_cap_enabled": per_coin_size_cap_enabled,
        "per_coin_size_cap_pct": per_coin_size_cap_pct,
        # ★ Conviction Override Slot (2026-05-10 부모님 결정)
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
        # ★ BE Stall Exit (2026-05-14 부모 — UI 노출)
        "be_stall_exit_enabled": be_stall_exit_enabled,
        "be_stall_exit_sec": be_stall_exit_sec,
        "be_stall_intelligent_enabled": be_stall_intelligent_enabled,
        "be_stall_intelligent_rsi_strong": be_stall_intelligent_rsi_strong,
        "be_stall_intelligent_rsi_weak": be_stall_intelligent_rsi_weak,
        # ★ Pre-BE Stall Exit (2026-04-23 부모 지시)
        "pre_be_stall_exit_mode": pre_be_stall_exit_mode,
        "pre_be_stall_min_profit_pct": pre_be_stall_min_profit_pct,
        "pre_be_stall_sec": pre_be_stall_sec,
        "pre_be_stall_volatility_threshold_pct": pre_be_stall_volatility_threshold_pct,
        "pre_be_stall_max_since_peak_sec": pre_be_stall_max_since_peak_sec,
        # 🐢 Pre-BE 손실방지선 (2026-06-09 부모 "지금 나우")
        "pre_be_loss_guard_enabled": pre_be_loss_guard_enabled,
        "pre_be_loss_guard_peak_max_pct": pre_be_loss_guard_peak_max_pct,
        "pre_be_loss_guard_trigger_loss_pct": pre_be_loss_guard_trigger_loss_pct,
        "pre_be_loss_guard_min_hold_sec": pre_be_loss_guard_min_hold_sec,
        "pre_be_loss_guard_max_age_sec": pre_be_loss_guard_max_age_sec,
        # ★ Reverse Drift Exit (2026-05-16 부모 지시)
        "reverse_drift_exit_enabled": reverse_drift_exit_enabled,
        "reverse_drift_peak_min_pct": reverse_drift_peak_min_pct,
        "reverse_drift_peak_max_pct": reverse_drift_peak_max_pct,
        "reverse_drift_min_since_peak_sec": reverse_drift_min_since_peak_sec,
        "reverse_drift_max_since_peak_sec": reverse_drift_max_since_peak_sec,
        "reverse_drift_pct": reverse_drift_pct,
        "reverse_drift_atr_adaptive_enabled": reverse_drift_atr_adaptive_enabled,
        "reverse_drift_atr_multiplier": reverse_drift_atr_multiplier,
        "reverse_drift_atr_cap_pct": reverse_drift_atr_cap_pct,
        # ★ 끝물 추격 차단 (Overextension) — 2026-06-07 부모
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
        # 🎯 변곡 setup 점수 (2026-06-12)
        "inflection_setup_enabled": inflection_setup_enabled,
        "inflection_setup_weight": inflection_setup_weight,
        "inflection_setup_cap": inflection_setup_cap,
        "inflection_setup_base": inflection_setup_base,
        "inflection_setup_slope_scale": inflection_setup_slope_scale,
        # 🎣 Retest setup 점수 (2026-06-12)
        "retest_setup_enabled": retest_setup_enabled,
        "retest_setup_weight": retest_setup_weight,
        "retest_setup_turn_bonus": retest_setup_turn_bonus,
        "retest_retr_lo": retest_retr_lo,
        "retest_retr_hi": retest_retr_hi,
        # 🌋 변동성 각성 SL 적응 (2026-06-11)
        "awaken_sl_enabled": awaken_sl_enabled,
        "awaken_sl_mode": awaken_sl_mode,
        "awaken_atr_ratio": awaken_atr_ratio,
        "awaken_atr_lookback": awaken_atr_lookback,
        "awaken_max_sl_mult": awaken_max_sl_mult,
        "awaken_require_day_align": awaken_require_day_align,
        "awaken_swing_lookback": awaken_swing_lookback,
        "awaken_atr_buffer": awaken_atr_buffer,
        # ② 끝물 상한 감점 (2026-06-09)
        "conviction_ceiling_enabled": conviction_ceiling_enabled,
        "conviction_ceiling_start": conviction_ceiling_start,
        "conviction_ceiling_target": conviction_ceiling_target,
        "conviction_ceiling_adx_exempt": conviction_ceiling_adx_exempt,
        # ★ 이윤 여력 페널티 (2026-06-09)
        "headroom_penalty_enabled": headroom_penalty_enabled,
        "headroom_sr_penalty": headroom_sr_penalty,
        "headroom_sr_near_pct": headroom_sr_near_pct,
        "headroom_rsi_penalty": headroom_rsi_penalty,
        "headroom_rsi_overbought": headroom_rsi_overbought,
        "headroom_rsi_oversold": headroom_rsi_oversold,
        "headroom_bb_penalty": headroom_bb_penalty,
        "headroom_bb_hi_pctb": headroom_bb_hi_pctb,
        "headroom_bb_lo_pctb": headroom_bb_lo_pctb,
        # 🌊 거시하락 능동 SHORT 진입 2단계 (2026-06-11)
        "macro_short_timing_enabled": macro_short_timing_enabled,
        "macro_short_timing_delta": macro_short_timing_delta,
        "macro_short_timing_min_signals": macro_short_timing_min_signals,
        "macro_short_timing_bounce_pct": macro_short_timing_bounce_pct,
        "macro_short_timing_lookback": macro_short_timing_lookback,
        # ★ 레짐역행 보유탈출 P3 (router 배선 누락 fix 2026-06-07)
        "macro_exit_enabled": macro_exit_enabled,
        "macro_exit_breadth_min": macro_exit_breadth_min,
        "macro_exit_sl_cushion_pct": macro_exit_sl_cushion_pct,
        "macro_exit_strong_coin_exempt": macro_exit_strong_coin_exempt,
        "macro_exit_exempt_min_roe": macro_exit_exempt_min_roe,
        # ★ router 배선 누락 일괄 fix (2026-06-07) — 12필드
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
        # ★ [2026-05-18 부모 비전 #6] Entry Grace Period + Market Bias Grace Exit + News Grace Exit
        "entry_grace_period_sec": entry_grace_period_sec,
        "market_bias_grace_exit_enabled": market_bias_grace_exit_enabled,
        "news_grace_exit_enabled": news_grace_exit_enabled,
        "news_grace_exit_threshold": news_grace_exit_threshold,
        # ★★★★ [2026-05-18 부모 비전 #6 B 옵션] 시간 무관 OR 조건
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
        # ★ Entry Expectation (2026-05-14 부모 — 진입 기대치 메커니즘)
        "entry_expectation_enabled": entry_expectation_enabled,
        "expectation_progress_exit_enabled": expectation_progress_exit_enabled,
        "expectation_progress_t1_min": expectation_progress_t1_min,
        "expectation_progress_t1_pct": expectation_progress_t1_pct,
        "expectation_progress_t2_min": expectation_progress_t2_min,
        "expectation_progress_t2_pct": expectation_progress_t2_pct,
        # ★ 음수 progress 즉시 컷 (2026-05-15 부모)
        "expectation_progress_neg_cut_enabled": expectation_progress_neg_cut_enabled,
        "expectation_progress_neg_cut_pct": expectation_progress_neg_cut_pct,
        "expectation_progress_neg_cut_min": expectation_progress_neg_cut_min,
        # ★ Entry Quality Gates (2026-05-15 부모)
        "entry_expectation_gate_enabled": entry_expectation_gate_enabled,
        "entry_expectation_min_rr": entry_expectation_min_rr,
        "entry_expectation_min_reward_pct": entry_expectation_min_reward_pct,
        "entry_expectation_max_risk_pct": entry_expectation_max_risk_pct,
        # ★ 거시 레짐 방향 게이트 (2026-06-02 부모)
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
        # ★ TF 추세 가중 (2026-06-03 부모)
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
        # ★ Min TP fee-guard (2026-05-15 부모)
        "min_tp_distance_enabled": min_tp_distance_enabled,
        "min_tp_distance_pct": min_tp_distance_pct,
        # ★ 5m Microtiming Gate (2026-05-16 부모)
        "microtiming_5m_enabled": microtiming_5m_enabled,
        "microtiming_5m_min_score": microtiming_5m_min_score,
        "microtiming_5m_defer_sec": microtiming_5m_defer_sec,
        "microtiming_5m_max_defers": microtiming_5m_max_defers,
        "microtiming_5m_rsi_long_threshold": microtiming_5m_rsi_long_threshold,
        "microtiming_5m_rsi_short_threshold": microtiming_5m_rsi_short_threshold,
        "microtiming_5m_bb_low_pct": microtiming_5m_bb_low_pct,
        "microtiming_5m_bb_recover_pct": microtiming_5m_bb_recover_pct,
        "microtiming_5m_phase_k_exempt": microtiming_5m_phase_k_exempt,
        # ★ DrawdownShield base (2026-05-16 부모)
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
        # ★ [2026-05-16 부모] Same-coin Flip Cooldown + 5m Raw Body Guard + Imminent Flip
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
        # ★ [2026-05-18 부모 비전 #5] Leading Entry
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
        # ★ [2026-05-19] BB 차단 가드 임계
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
        # ★ Leverage Tier (ATR 기반 차등, 2026-04-25)
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
        # Event Shield (경제이벤트 방패)
        "event_shield_enabled": event_shield_enabled,
        "event_shield_times_kst": event_shield_times_kst,
        "event_shield_window_min": event_shield_window_min,
        "event_shield_lead_min": event_shield_lead_min,
        "event_shield_lock_pct": event_shield_lock_pct,
        "event_shield_auto_fetch": event_shield_auto_fetch,
        # Auto Take-Profit (트레일링 거두기) / Stop-Loss (2026-06-08 부모 승자 거두기)
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
        # [2026-05-19] Advanced 124개
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
        # ★ [2026-05-18] Consecutive Loss Pause (옛 누락 추가)
        "consecutive_loss_pause_enabled": consecutive_loss_pause_enabled,
        "consecutive_loss_pause_count": consecutive_loss_pause_count,
        "consecutive_loss_pause_min": consecutive_loss_pause_min,
        "regime_direction_fail_enabled": regime_direction_fail_enabled,
        "regime_direction_fail_window_hours": regime_direction_fail_window_hours,
        "regime_direction_fail_max": regime_direction_fail_max,
        "btc_regime_enabled": btc_regime_enabled,
        "btc_regime_bear_long_delta": btc_regime_bear_long_delta,
        # 입력 시 bull_short_delta 도 동일 값 적용 (역방향 페널티 통일)
        "btc_regime_bull_short_delta": btc_regime_bear_long_delta,
        "market_bias_enabled": market_bias_enabled,
        "mb_against_delta": mb_against_delta,
        "sess_quiet_delta": sess_quiet_delta,
        "sess_active_delta": sess_active_delta,
        "dm_loss_count_delta": dm_loss_count_delta,
        # B11 Regime Direction Lock (2026-04-19 하드 차단, 3축 토글)
        "regime_direction_lock_enabled": regime_direction_lock_enabled,
        "regime_lock_use_slope": regime_lock_use_slope,
        "regime_lock_use_distance": regime_lock_use_distance,
        "regime_lock_use_cross": regime_lock_use_cross,
        "regime_direction_lock_freeze_sec": regime_direction_lock_freeze_sec,
        "regime_direction_lock_neutral_block": regime_direction_lock_neutral_block,
        # ★ B12 Scanner Breadth Lock (2026-04-23 부모 지시)
        "regime_lock_mode": regime_lock_mode,
        "b12_threshold_n": b12_threshold_n,
        "b12_window_sec": b12_window_sec,
        # 방향별 슬롯 상한 (2026-04-20 형 지시)
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
        # ★ Phase J v2 (2026-04-21): ADX 하락 skip
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
        # 🪙 Orderbook 깊이 사이즈 적응 (2026-06-09)
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
        # ★★★ [2026-05-27 부모 정신] Phase 6 — H4/H1 PA + 점수 통합 + 5M 긴급탈출 ★★★
        # α. Phase 6 점수 통합 (Master)
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
        # ── ★ [2026-06-03 부모] D1 추세 가중 + 갭 체크 게이트 ──
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
        # ── ★ [2026-06-15 해결안 B·C] 관측 토글 ──
        "gate_ledger_enabled": gate_ledger_enabled,
        "dual_observe_auto_off_weak": dual_observe_auto_off_weak,
    }.items():
        if v is not None:
            patch[k] = v

    # ★ [2026-05-18] leading_entry_mode 정규화 + 검증
    if leading_entry_mode is not None:
        _le_norm = str(leading_entry_mode).strip().upper()
        if _le_norm not in ("OFF", "CFID", "PATTERN"):
            return {"ok": False, "error": f"leading_entry_mode must be OFF/CFID/PATTERN (got '{leading_entry_mode}')"}
        patch["leading_entry_mode"] = _le_norm

    # scanner_blacklist: 쉼표 구분 문자열 → 리스트 변환
    if scanner_blacklist is not None:
        if scanner_blacklist.strip() == "":
            patch["scanner_blacklist"] = []
        else:
            patch["scanner_blacklist"] = [s.strip().upper() for s in scanner_blacklist.split(",") if s.strip()]

    # ★ [2026-05-31 부모 server-b race fix] lock_market 명시적 제공 시만 update (None=부분 호출 → 옛값 유지).
    #   부모님 Save Config 시 JS 가 항상 빈 string 보냄 → 여기서 빈 string 저장 → 다음 polling 빈값.
    if lock_market is not None:
        patch["lock_market"] = lock_market.strip().upper()

    result = fm.update_config(patch)
    # ★ [2026-05-19] 자동 snapshot 저장 (silent fail — config 변경은 무영향)
    # ★ [2026-06-20] 백그라운드 스레드 — 스냅샷이 _calc_pnl_24h(저널 풀파싱, 느린 서버 ~33s)를 호출해
    #    config 저장 POST 가 그만큼 블록되던 근본 fix (드로다운 리셋 2s vs config 33s). 스냅샷=비필수라 응답 안 막음.
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
        logger.warning("[FOCUS_API] Scan failed: %s", exc)
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
    wait_for_signal: bool = Query(False, description="[2026-05-29 운영자] True 시 즉시 X. 운영자 방향 신호 확인 후 자동 실행 (default timeout 1시간)"),
    timeout_sec: Optional[float] = Query(None, description="Smart Manual Entry 대기 시간 (default 3600s)"),
):
    """[2026-05-16 부모] 수동 강제 진입 — 게이트 우회 (microtiming/EE/MTF FLIP).

    안전 가드는 유지: reentry / Bybit duplicate / cross-strategy / qty/margin.
    부모님이 시스템 판단을 무시하고 직접 진입할 때 사용.

    [2026-05-29 부모] wait_for_signal=True 시 신호 확인 대기 모드.
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
    """[2026-05-29 부모] Smart Manual Entry 대기 큐 조회."""
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
    """[2026-05-29 부모] Smart Manual Entry 큐 취소."""
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
    # lock 설정 시 즉시 해당 코인으로 전환
    if fm.config.lock_market:
        fm.selected_market = fm.config.lock_market
        if fm.state.value in ("DORMANT", "ALERT", "COOLDOWN"):
            fm.state = FocusState.HUNT  # 다음 tick에서 HUNT 로직 실행
    else:
        logger.info("[FOCUS] Market unlocked — will auto-scan")
    fm._save_config()
    return {
        "ok": True,
        "lock_market": fm.config.lock_market,
        "message": f"Locked to {fm.config.lock_market}" if fm.config.lock_market else "Unlocked — auto-scan resumed",
    }


# ── Top 10 Live Scanner ─────────────────────────────────────

# [2026-06-12] 스캔결과 단기 캐시 — 재오픈/다중 탭이 매번 11코인 풀스캔(코인당
# greenpen+conviction+guard, 수십 API)을 재실행하지 않게. TTL 15초.
# ★ engine_warnings(E-STOP/stale)는 캐시 hit 에도 매번 fresh 재계산 → 경고는 안 묵음.
_SCAN_RESULT_TTL = 15.0
_SCAN_RESULT_CACHE: dict = {}  # top_n -> (ts, results_list)


def _scan_engine_warnings(request, fm) -> list:
    """엔진 메타 경고 (E-STOP / Scanner stale / FOCUS off) — 항상 fresh, API 호출 없음."""
    import time as _time
    warnings = []
    try:
        _sys = request.app.state.system
        if getattr(_sys, 'emergency_stop', False):
            warnings.append({'level': 'critical', 'tag': 'E_STOP', 'msg': '🆘 Emergency Stop ACTIVE — 모든 진입 차단 (Resume 필요)'})
    except Exception:
        pass
    try:
        _last = float(getattr(fm, 'last_scan_ts', 0) or 0)
        if _last > 0:
            _stale = _time.time() - _last
            if _stale > 180:  # 3분+ stale
                warnings.append({'level': 'warn', 'tag': 'SCAN_STALE', 'msg': f'⚠️ Scanner stale {int(_stale)}s — 엔진 멈춤 의심'})
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
        from app.core.constants import BYBIT_MARKET_TICKERS, parse_bybit_list
        from app.core.rate_limiter import bybit_get
        import time as _scan_t

        # ── #2 스캔결과 캐시 hit (재오픈/다중탭 즉시화) ──
        _ck = int(top_n)
        _hit = _SCAN_RESULT_CACHE.get(_ck)
        if _hit and (_scan_t.time() - _hit[0]) < _SCAN_RESULT_TTL:
            _items = _hit[1]
            return {"ok": True, "items": _items, "count": len(_items),
                    "engine_warnings": _scan_engine_warnings(request, fm),
                    "cached": True, "cache_age": round(_scan_t.time() - _hit[0], 1)}

        # 1) Get top coins by 24h turnover (linear)
        resp = bybit_get(BYBIT_MARKET_TICKERS, params={"category": "linear"}, timeout=10)
        resp.raise_for_status()
        tickers = parse_bybit_list(resp.json())

        # ★ [2026-06-13 부모] 그린팬 스캐너도 진입 필터(scanner_min_price_usdt)와 일관 — 저가코인 제외
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
            if _scan_min_price > 0 and 0 < price < _scan_min_price:  # ★ 저가코인 스캐너에서도 제외
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

                # PA 패턴 없을 때 → Market Structure 폴백
                if pa_type == "none":
                    _struct = gp.structure
                    _s_conf = round(float(getattr(_struct, "confidence", 0) or 0) * 100)

                    # BOS(Break of Structure) 감지 시 우선 표시
                    _bos = getattr(_struct, "bos", None)
                    if _bos and getattr(_bos, "detected", False):
                        pa_name = f"BOS_{_bos.direction}"
                        conf = max(_s_conf, 60)
                        pa_type = "bos"
                        signal = "BUY" if _bos.direction == "BULLISH" else "SELL"
                    elif _struct.trend.value != "SIDEWAYS":
                        # 추세 중: 최근 스윙 패턴 표시 (HH/HL or LH/LL)
                        _swings = getattr(_struct, "swings", []) or []
                        if len(_swings) >= 2:
                            _last2 = [s.type.value for s in _swings[-2:]]
                            pa_name = "/".join(_last2)
                        else:
                            pa_name = "TREND"
                        conf = _s_conf
                        pa_type = "structure"
                    else:
                        # 횡보: SW range 표시
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

                # ── Conviction score (★ 2026-05-11 Phase 1 통합) ──
                # focus_manager 의 _compute_conviction_score 직접 호출 — 진입 결정 점수와 일치
                # PA Pattern (0~6) + Phase 1 감점 (MTF Conflict + Momentum Reversal) 모두 반영
                _direction = "LONG" if signal == "BUY" else ("SHORT" if signal == "SELL" else "")
                try:
                    # zones 도 전달 (PA Pattern 의 zone bonus 적용용)
                    _zones_tuple = None
                    try:
                        _zones_list = getattr(gp, "zones", []) or []
                        if _zones_list:
                            _first = _zones_list[0]
                            _zones_tuple = (float(_first.price_low), float(_first.price_high))
                    except Exception:
                        _zones_tuple = None
                    _conv = fm._compute_conviction_score(symbol, candles, direction=_direction, zones=_zones_tuple)
                    # ★ [2026-05-17] breakdown 즉시 copy — 다음 코인 평가 시 덮어쓰임 방지
                    _conv_dbg = dict(getattr(fm, '_last_conviction_breakdown', {}) or {})
                    # ★ [2026-05-17] Scanner 적용 최종 conviction 있으면 우선 사용 (BB 위치 감점 등 반영)
                    _scan_final_conv = (getattr(fm, '_last_scan_conviction', {}) or {}).get(symbol)
                    if _scan_final_conv is not None:
                        _conv = _scan_final_conv
                except Exception:
                    # Fallback: 단순 ADX 기반 (예전 logic)
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

                # ★ [2026-05-17] Scanner cycle 차단 이유 (UI STATUS 칼럼)
                _block_reason = ""
                try:
                    _scan_cache = getattr(fm, '_last_scan_filter', None) or {}
                    _block_reason = (_scan_cache.get('items', {}) or {}).get(symbol, "")
                except Exception:
                    pass

                # ── [2026-05-20 Phase 6 Stage 6] Energy bar 변화율 + 시계열 (UI sparkline 용) ──
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
                        # 최근 N개만 sparkline 용 (timestamp 생략, confidence% 만)
                        _conf_history = [round(float(e[2]) * 100, 1) for e in list(_hist_deque)[-30:]]
                except Exception:
                    pass

                # ★ [2026-05-28] 가드 점수 캐시 (_evaluate_entry 의 guard_score 평가 결과)
                _gs = dict((getattr(fm, '_last_guard_score', {}) or {}).get(symbol.upper(), {}) or {})
                # 캐시 없으면 BUY/SELL 코인에 한해 직접 평가 (부모 5-28: 모든 행 4 컬럼)
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
                        logger.debug("[FOCUS_API] %s guard_score eval failed: %s", symbol, _gse_tn)
                # ★ [2026-05-28 부모] 봇 의견 경고 배지 (BB 충돌/추세 충돌/점수 음수 등)
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
                    "confidence_delta_pp": _conf_delta_pp,        # ★ Phase 6 Stage 6: 5분간 변화율 (%p)
                    "confidence_samples": _conf_samples,          # 시계열 데이터 포인트 수
                    "confidence_history": _conf_history,          # 최근 30개 confidence % (sparkline)
                    "atr": round(gp.atr, 2),
                    "adx": _adx_val,
                    "zones": len(gp.zones),
                    "conviction": _conv,
                    "conviction_breakdown": _conv_dbg,  # ★ Phase 5 항목별 점수 (UI tooltip)
                    "block_reason": _block_reason,  # ★ Scanner cycle 차단 이유
                    "status": _status,
                    # ★ [2026-05-28] 가드 점수 분리 컬럼용
                    "guard_base": _gs.get("base"),
                    "guard_deduction": _gs.get("deduction"),
                    "guard_total": _gs.get("total"),
                    "guard_threshold": _gs.get("threshold"),
                    "guard_breakdown": _gs.get("breakdown"),
                    "bot_opinion": _bot_op,  # ★ [2026-05-28] 봇 의견 (없으면 None)
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

        # ★ lock_market이 Top N에 없으면 별도 분석 추가 (부모 5-28: 비어있어도 금 기본 첫 행)
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
                    # lock_market 가격 정보
                    _lk_price = _lk_candles[-1].close if _lk_candles else 0
                    # conviction for lock_market (★ 2026-05-11 Phase 1 통합)
                    _lk_direction = "LONG" if _lk_signal == "BUY" else ("SHORT" if _lk_signal == "SELL" else "")
                    try:
                        _lk_zones_tuple = None
                        _lk_zones_list = getattr(_lk_gp, "zones", []) or []
                        if _lk_zones_list:
                            _lk_first = _lk_zones_list[0]
                            _lk_zones_tuple = (float(_lk_first.price_low), float(_lk_first.price_high))
                        _lk_conv = fm._compute_conviction_score(_lock, _lk_candles, direction=_lk_direction, zones=_lk_zones_tuple)
                        _lk_conv_dbg = dict(getattr(fm, '_last_conviction_breakdown', {}) or {})
                        # ★ Scanner 최종 conviction 우선
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
                    # ★ [2026-05-28 부모] lock_market 도 guard_score 평가 — 4 컬럼 채우기
                    _lk_gs_data = {}
                    try:
                        # 캐시 우선 (이미 _evaluate_entry 통과한 경우)
                        _lk_gs_data = dict((getattr(fm, '_last_guard_score', {}) or {}).get(_lock, {}) or {})
                        # 캐시 없거나 방향 부합 시 직접 평가
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
                        logger.debug("[FOCUS_API] lock_market guard_score eval failed: %s", _gse)
                    # ★ [2026-05-28] 봇 의견 (lock_market 도 적용)
                    _lk_bot_op = _bot_opinion(_lk_signal, _lk_gp.structure.trend.value,
                                              _lk_gs_data.get("total"), _lk_gs_data.get("threshold"),
                                              _lk_block, _lk_pa)
                    results.append({
                        "market": _lock, "signal": _lk_signal, "pa_pattern": _lk_pa,
                        "pa_type": _lk_pa_type, "trend": _lk_gp.structure.trend.value,
                        "confidence": _lk_conf, "atr": round(_lk_gp.atr, 2),
                        "adx": _lk_adx, "zones": len(_lk_gp.zones),
                        "conviction": _lk_conv,
                        "conviction_breakdown": _lk_conv_dbg,  # ★ Phase 5 항목별 점수
                        "block_reason": _lk_block,  # ★ Scanner cycle 차단 이유
                        "status": _lk_status,
                        # ★ [2026-05-28] guard_score 분리 컬럼
                        "guard_base": _lk_gs_data.get("base"),
                        "guard_deduction": _lk_gs_data.get("deduction"),
                        "guard_total": _lk_gs_data.get("total"),
                        "guard_threshold": _lk_gs_data.get("threshold"),
                        "guard_breakdown": _lk_gs_data.get("breakdown"),
                        "bot_opinion": _lk_bot_op,  # ★ [2026-05-28] 봇 의견
                        "price": _lk_price, "change_pct": 0,
                        "_is_lock": True,  # 정렬용 마커
                    })
            except Exception as _lke:
                logger.debug("[FOCUS_API] lock_market scan failed: %s", _lke)

        # Sort: lock_market 항상 첫 행 (부모 5-28 "금 전용 공간") → BUY/SELL → confidence
        signal_order = {"BUY": 0, "SELL": 1, "HOLD": 2, "ERR": 3, "-": 4}
        results.sort(key=lambda x: (
            0 if (x.get("_is_lock") or x.get("market", "").upper() == _lock) else 1,
            signal_order.get(x["signal"], 9),
            -x["confidence"]
        ))

        # ── #2 스캔결과 캐시 저장 (성공 시만 — 에러 캐시 오염 방지) ──
        _SCAN_RESULT_CACHE[_ck] = (_scan_t.time(), results)

        # ★ [2026-05-17] 엔진 메타 상태 — 부모님 9개월 트라우마 ("엔진/E-STOP/Auto Engine") 대응
        # ★ 항상 fresh (캐시 hit 경로와 동일 헬퍼) → E-STOP/stale 경고는 안 묵음.
        engine_warnings = _scan_engine_warnings(request, fm)
        return {"ok": True, "items": results, "count": len(results), "engine_warnings": engine_warnings}
    except Exception as exc:
        logger.warning("[FOCUS_API] scan-list failed: %s", exc)
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
    """7개 TF (D/H4/H1/30M/15M/5M/3M) 의 진행 중 봉 정보 — 수동 진입 참고용.

    [2026-05-21] 부모님 결정. 봉 닫히기 전 흐름 시각화. 진입 로직 변경 없음.
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
# Trade Journal — FOCUS + Harpoon 장부
# ============================================================

# Peer Brief Scanner 응답 캐시 — near-miss 사후판정 enrichment 가 near_miss 마다
# 현재가+5/15/30/60분 캔들을 fetch 해 무겁다. 여러 탭/서버가 20초마다 폴링하면 kline 벽에
# 걸려 응답이 늦어졌다 빨라졌다(=패널 깜빡임). 함대 전체 동일 페이로드라 짧은 TTL 로 공유 재사용.
# [2026-06-19 부모 "매끄럽지 못하다"] = 대시보드 느림 안티패턴(저널leak→b12풀파싱→kline벽)의 4번째.
_PEER_CACHE_RESP_BOX: dict = {"ts": 0.0, "data": None}
_PEER_CACHE_RESP_TTL = 25.0   # 초 — 대시보드 폴링 20초보다 약간 위라야 연속 폴링이 적중(단일 탭도 매끄럽게).
                              #       사후판정은 5~60분 단위라 25초 staleness 는 무시 가능.


@router.get("/peer-cache")
def focus_peer_cache(request: Request):
    """Peer Brief Scanner 용 — 옆 서버 캐시 + 자기(Home) brief 통합 (읽기전용, 추가 폴링 X).
    servers[] = [self, peer1, peer2...] 균일 형식: positions/near_miss/losses/wins (2026-06-07 부모).
    ★ 응답을 _PEER_CACHE_RESP_TTL 초 캐시 → 다중 탭/폴링이 무거운 enrichment 1회를 재사용(매끄러움)."""
    import time as _t
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
            return ("unknown", "판정대기")
        if age_min < 5.0:
            return ("watching", "관찰중")
        # ret_now = 막힌 방향으로 들어갔다면 현재 어느 정도 갔는가.
        # +면 놓친 수익, 0/음수면 막은 게 유리했거나 횡보.
        if ret_now > 0.10:
            return ("missed_entry", "아쉬운 차단")
        if ret_now <= 0.05:
            return ("good_block", "좋은 차단")
        return ("neutral", "중립")

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

    # self (Home) 먼저
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
        except Exception as exc:  # noqa: BLE001 — enrichment 실패해도 raw near_miss 유지(패널 안 비게)
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
    """FOCUS + Harpoon 거래 장부 조회 (페이지네이션 + 코인 필터)."""
    try:
        from app.manager.trade_journal import journal
        result = journal.get_trades(
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
    """사용 가능한 마켓 목록 조회."""
    try:
        from app.manager.trade_journal import journal
        markets = journal.get_markets()
        return {"ok": True, "markets": markets}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@router.get("/journal/summary")
def focus_journal_summary():
    """FOCUS + Harpoon 성과 요약 (Dynamic Trailing 비교 포함)."""
    try:
        from app.manager.trade_journal import journal
        summary = journal.get_summary()
        return {"ok": True, "summary": summary}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ── Daily Performance Snapshots ────────────────────────────

@router.get("/daily-snapshots")
def focus_daily_snapshots():
    """저장된 일별 성과 스냅샷 전체 조회 (차트용)."""
    try:
        from app.manager.focus_daily_snapshot import get_all_snapshots
        snapshots = get_all_snapshots()
        return {"ok": True, "snapshots": snapshots, "count": len(snapshots)}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@router.get("/daily-snapshots/{date}")
def focus_daily_snapshot_detail(date: str):
    """특정 날짜 스냅샷 상세 조회."""
    try:
        from app.manager.focus_daily_snapshot import load_snapshot
        snap = load_snapshot(date)
        if snap is None:
            return {"ok": False, "error": f"No snapshot for {date}"}
        return {"ok": True, "snapshot": snap}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@router.post("/daily-snapshots/backfill")
def focus_daily_snapshot_backfill(request: Request):
    """journal에서 과거 스냅샷 일괄 생성 (빠진 날짜만)."""
    try:
        from app.manager.focus_daily_snapshot import backfill_from_journal
        fm = _get_fm(request)
        from dataclasses import asdict
        config = asdict(fm.config)
        count = backfill_from_journal(config)
        return {"ok": True, "created": count}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@router.post("/daily-snapshots/save-today")
def focus_daily_snapshot_save_today(request: Request):
    """현재 진행 중인 오늘 스냅샷을 수동 저장."""
    try:
        from app.manager.focus_daily_snapshot import build_snapshot, save_snapshot
        from app.manager.trade_journal import JOURNAL_PATH
        from dataclasses import asdict
        import datetime as _dt
        import json as _json

        fm = _get_fm(request)

        # 오늘 리셋 기준선
        now_utc = _dt.datetime.now(_dt.timezone.utc)
        boundary = now_utc.replace(hour=22, minute=0, second=0, microsecond=0)
        if now_utc.hour < 22:
            boundary -= _dt.timedelta(days=1)

        # journal EXIT 거래 로드
        exits = []
        import os
        if os.path.exists(JOURNAL_PATH):
            with open(JOURNAL_PATH, "r", encoding="utf-8") as f:
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
            boundary.timestamp() + 86400,  # 다음 리셋까지
            asdict(fm.config),
        )
        path = save_snapshot(snap)
        return {"ok": True, "snapshot": snap, "path": path}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ── Capital Tracking (입출금 + 순수 성과) ──────────────────

@router.post("/capital/initial")
def capital_set_initial(amount: float = Query(..., description="초기 자본 (USDT)")):
    """초기 자본 설정 (처음 한 번)."""
    from app.manager.capital_tracker import capital_tracker
    return capital_tracker.set_initial(amount)


@router.post("/capital/deposit")
def capital_deposit(
    amount: float = Query(..., description="입금액 (USDT)"),
    memo: str = Query("", description="메모"),
):
    """입금 기록."""
    from app.manager.capital_tracker import capital_tracker
    return capital_tracker.deposit(amount, memo)


@router.post("/capital/withdraw")
def capital_withdraw(
    amount: float = Query(..., description="출금액 (USDT)"),
    memo: str = Query("", description="메모"),
):
    """출금 기록."""
    from app.manager.capital_tracker import capital_tracker
    return capital_tracker.withdraw(amount, memo)


@router.get("/capital/performance")
def capital_performance(request: Request):
    """순수 트레이딩 성과 조회 (입출금 보정)."""
    try:
        from app.manager.capital_tracker import capital_tracker
        from app.manager.trade_journal import journal

        fm = _get_fm(request)

        # 현재 Bybit 잔고
        equity = fm._get_available_margin() or 0

        # journal 총 실현 PnL
        summary = journal.get_summary()
        trading_pnl = summary.get("combined", {}).get("total_pnl", 0)

        perf = capital_tracker.get_performance(equity, trading_pnl)
        return {"ok": True, **perf}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@router.get("/capital/events")
def capital_events():
    """입출금 이벤트 목록."""
    from app.manager.capital_tracker import capital_tracker
    events = capital_tracker.get_events()
    return {"ok": True, "events": events, "count": len(events)}


@router.get("/capital/status")
def capital_status():
    """자본 추적 상태."""
    from app.manager.capital_tracker import capital_tracker
    return {"ok": True, **capital_tracker.get_status()}


# ── Time Analytics (요일별 + 시간대별 실적) ─────────────────────

@router.get("/analytics/by-dow")
def analytics_by_dow():
    """요일별 실적 분석 (KST 기준)."""
    import json as _json, datetime as _dt, os
    journal_path = "runtime/focus_harpoon_journal.jsonl"
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
    """4시간 슬롯별 실적 (KST 07:00 시작)."""
    import json as _json, datetime as _dt, os
    journal_path = "runtime/focus_harpoon_journal.jsonl"
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
# 형의 답례 선물 🎁 — Weekly Intelligence / Coin Report Card /
#   Correlation Guard / Drawdown Shield / Twin Battle
# ════════════════════════════════════════════════════════════════

# ── Weekly Intelligence ─────────────────────────────────────────

@router.get("/weekly-report")
def weekly_report_current():
    """현재 주간 인텔리전스 리포트."""
    from app.manager.weekly_intelligence import generate_current_week_report, generate_recommendations
    report = generate_current_week_report()
    if not report:
        return {"ok": False, "message": "스냅샷 데이터 부족 (최소 1일 필요)"}
    report["recommendations"] = generate_recommendations(report)
    return {"ok": True, **report}


@router.get("/weekly-report/{week}")
def weekly_report_by_week(week: str):
    """특정 주간 리포트 조회. week 형식: 2026-W16"""
    from app.manager.weekly_intelligence import load_weekly_report
    report = load_weekly_report(week)
    if not report:
        return {"ok": False, "message": f"{week} 리포트 없음"}
    return {"ok": True, **report}


@router.get("/weekly-reports")
def weekly_report_all():
    """전체 주간 리포트 목록."""
    from app.manager.weekly_intelligence import get_all_weekly_reports
    return {"ok": True, "reports": get_all_weekly_reports()}


@router.post("/weekly-report/generate")
def weekly_report_generate(force: bool = Query(False)):
    """지난 주 리포트 수동 생성."""
    from app.manager.weekly_intelligence import auto_generate_weekly
    report = auto_generate_weekly(force=force)
    if not report:
        return {"ok": False, "message": "생성 실패 또는 이미 존재 (force=true로 재생성)"}
    return {"ok": True, **report}


# ── Coin Report Card ────────────────────────────────────────────

@router.get("/coin-grades")
def coin_grades():
    """코인별 성적표 — 등급 + 점수 + 통계."""
    from app.manager.coin_report_card import coin_report_card
    return {"ok": True, **coin_report_card.get_full_report()}


@router.get("/coin-grades/{coin}")
def coin_grade_detail(coin: str):
    """특정 코인 성적 상세."""
    from app.manager.coin_report_card import coin_report_card
    report = coin_report_card.get_full_report()
    coin_upper = coin.upper()
    if coin_upper not in report.get("coins", {}):
        return {"ok": False, "message": f"{coin_upper} 데이터 없음"}
    return {"ok": True, "coin": coin_upper, **report["coins"][coin_upper]}


@router.post("/coin-grades/refresh")
def coin_grades_refresh(days: int = Query(7, ge=1, le=90, description="분석 기간 (일)")):
    """코인 성적표 새로고침."""
    from app.manager.coin_report_card import CoinReportCard
    card = CoinReportCard(lookback_days=days)
    result = card.refresh()
    return {"ok": True, **result}


# ── Correlation Guard ───────────────────────────────────────────

@router.get("/correlation/check")
def correlation_check(
    coin: str = Query(..., description="진입 예정 코인 (예: ETHUSDT)"),
    direction: str = Query(..., description="LONG 또는 SHORT"),
    request: Request = None,
):
    """새 코인 진입 시 상관관계 감점 확인."""
    from app.manager.correlation_guard import correlation_guard
    fm = _get_fm(request)
    positions = [{"market": p.market, "direction": p.direction} for p in fm.positions]
    result = correlation_guard.check_entry(coin.upper(), direction.upper(), positions)
    return {"ok": True, **result}


@router.get("/correlation/exposure")
def correlation_exposure(request: Request):
    """현재 포지션 상관관계 노출도."""
    from app.manager.correlation_guard import correlation_guard
    fm = _get_fm(request)
    positions = [{"market": p.market, "direction": p.direction} for p in fm.positions]
    return {"ok": True, "exposure": correlation_guard.get_exposure_map(positions)}


@router.get("/correlation/matrix")
def correlation_matrix():
    """상관관계 매트릭스 (정적 + 동적)."""
    from app.manager.correlation_guard import correlation_guard
    return {"ok": True, **correlation_guard.get_correlation_matrix()}


# ── Drawdown Shield ─────────────────────────────────────────────

@router.get("/drawdown/status")
def drawdown_status():
    """드로다운 실드 현재 상태."""
    from app.manager.drawdown_shield import drawdown_shield
    return {"ok": True, **drawdown_shield.get_status()}


@router.get("/drawdown/history")
def drawdown_history():
    """일별 드로다운 기록."""
    from app.manager.drawdown_shield import drawdown_shield
    return {"ok": True, "history": drawdown_shield.get_history()}


@router.post("/drawdown/update")
def drawdown_update(
    current_equity: float = Query(..., description="현재 equity (USDT)"),
    today_pnl: float = Query(0, description="오늘 실현 PnL"),
):
    """드로다운 실드 수동 업데이트.

    [2026-04-18] PnL 기반 → Equity 기반 리팩토링.
    첫 인자가 current_pnl → current_equity로 변경됨.
    """
    from app.manager.drawdown_shield import drawdown_shield
    result = drawdown_shield.update(current_equity=current_equity, realized_pnl_today=today_pnl)
    return {"ok": True, **result}


@router.post("/drawdown/reset-cumulative")
def drawdown_reset_cumulative():
    """누적 워터마크 수동 리셋 — 관리자용.

    장기 정지/재시작 후 혹은 자본 변동(입금/출금) 시 사용.
    max_drawdown_pct/amount 까지 초기화되어 깨끗한 재출발.
    """
    from app.manager.drawdown_shield import drawdown_shield
    drawdown_shield.reset_cumulative()
    return {"ok": True, **drawdown_shield.get_status()}


@router.post("/drawdown/reset-daily")
def drawdown_reset_daily():
    """일간 피크/현재/드로다운 수동 리셋 — Phase G (2026-04-20 형 Claude 진단).

    reset_daily() 호출 누락 버그로 daily_peak_pnl 이 영원히 고착되어
    시장 좋아져도 CRISIS 페널티 -3 깔리는 현상 응급 해소용.
    내일 07:00 KST 부터는 _maybe_reset_daily_counters 안 자동 호출됨.
    """
    from app.manager.drawdown_shield import drawdown_shield
    drawdown_shield.reset_daily()
    return {"ok": True, **drawdown_shield.get_status()}


# ── Twin Battle ─────────────────────────────────────────────────

@router.get("/twin/export")
def twin_export():
    """형제 대결용 표준 스냅샷 내보내기."""
    from app.manager.twin_battle import twin_battle
    return {"ok": True, **twin_battle.export_snapshot()}


@router.post("/twin/compare")
def twin_compare(request: Request):
    """상대 서버 스냅샷과 비교. Body에 상대 export 데이터 전달."""
    import json as _json
    from app.manager.twin_battle import twin_battle
    try:
        body = _json.loads(request._receive.__self__._body.decode() if hasattr(request, '_receive') else '{}')
    except Exception:
        body = {}
    if not body:
        return {"ok": False, "message": "상대 서버 스냅샷 데이터를 Body에 전달해주세요"}
    result = twin_battle.compare(body)
    return {"ok": True, **result}


@router.post("/twin/name")
def twin_set_name(name: str = Query(..., description="서버 이름 (예: server-a, server-b)")):
    """서버 이름 설정."""
    from app.manager.twin_battle import twin_battle
    twin_battle.set_server_name(name)
    return {"ok": True, "server_name": name}


@router.get("/twin/status")
def twin_status():
    """Twin Battle 상태."""
    from app.manager.twin_battle import twin_battle
    return {"ok": True, **twin_battle.get_status()}


# ── Phase K Layer 3 — Regime Transition Watch UI ───────────────
# 자동거래 여부 무관 영구 감지기 (부모 통찰 2026-04-21).
# paper_mode=True 일 때는 JSONL 기록만, 실진입 X.
# UI 는 이 기록을 렌즈로 사용해 수동거래 보조.
# 형 letter #10 α 조건부 배포 (2026-04-21 20:10 KST).

# ── Phase L (2026-04-22) — S3 Fee-Aware Gate UI/Promotion ──────
# 형 letter #11 검수 기준 #6/#8/#10 + 승격 7 조건 자동 판정
# 부모 ① D paper_mode + ② A FOCUS 이식 + ③ A 1주 paper

@router.get("/s3-gate/promotion-status")
def s3_gate_promotion_status(request: Request):
    """S3 Gate 7일 paper 데이터 기반 enabled=True 승격 자격 자동 판정.

    형 letter #11 4절 승격 4 조건 (Track A) AND 검사:
      1. 7일 paper 최소 10건 skip 발동
      2. 가상 net_saved ≥ $20
      3. 실진입 net_vs_fee ≥ 0.5x
      4. LINK 5경로 중 0건 우회

    Track B (Harpoon) 추가 3 조건은 30일 후 별도 endpoint.
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

    # ★ cond_3 real_net_vs_fee 계산 (형 PASS letter 4-2 즉시 구현)
    # 최근 7일 FOCUS EXIT 의 sum(pnl_net) / sum(fee). 0.5x 이상 = 통과
    # 데이터 부족 (거래 5건 미만) 시 None → 보수적 False
    real_net_vs_fee = None
    real_net_vs_fee_data = {"trades": 0, "sum_gross": 0.0, "sum_fee": 0.0, "window_days": 7.0}
    try:
        from app.manager.trade_journal import journal as _j
        _resp = _j.get_trades(limit=500, strategy="FOCUS", market="", include_blocked=False, page=1) or {}
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
            _sum_fee += abs(_f)  # fee 는 양수로 통일
            _n += 1
        real_net_vs_fee_data["trades"] = _n
        real_net_vs_fee_data["sum_gross"] = round(_sum_gross, 2)
        real_net_vs_fee_data["sum_fee"] = round(_sum_fee, 2)
        if _n >= 5 and _sum_fee > 0:
            # ★ 정의 A 수정 (2026-04-22 21:35, paper 첫 응답 발견 모순):
            #   형 비교표 v2 의 0.16x = 115.73 / 726.67 = gross/fee (정의 A)
            #   동생 v2 코드는 (gross-fee)/fee (정의 B) 였음 — 형 자료와 불일치
            #   즉시 수정: gross / fee 로 통일
            real_net_vs_fee = round(_sum_gross / _sum_fee, 4)
            # 정의: net_vs_fee = gross / fee.
            #   0.16x = 형 9일 baseline (수수료 1$ 당 gross 16센트, 적자)
            #   0.5x  = 임계 (수수료 1$ 당 gross 50센트, 여전히 적자지만 절반 회복)
            #   1.0x  = 수수료 = gross (손익분기)
            #   1.5x+ = 수익권 (gross > fee × 1.5, 안정 수익)
    except Exception:
        pass  # journal 접근 실패 시 None 유지 (안전)

    cond_1_min_skips = totals.get("paper_skips", 0) >= 10
    cond_2_net_saved = virtual_net_saved >= 20.0
    cond_3_real_net_vs_fee = (real_net_vs_fee is not None and real_net_vs_fee >= 0.5)
    cond_4_link_bypass = link_bypass_count == 0

    conditions = {
        "cond_1_min_skips":         {"value": cond_1_min_skips, "actual": totals.get("paper_skips", 0), "target": 10},
        "cond_2_net_saved":         {"value": cond_2_net_saved, "actual": round(virtual_net_saved, 2), "target": 20.0},
        "cond_3_real_net_vs_fee":   {"value": cond_3_real_net_vs_fee, "actual": real_net_vs_fee, "target": 0.5,
                                     "data": real_net_vs_fee_data,
                                     "note": "최근 7일 FOCUS EXIT 의 sum_gross / sum_fee (정의 A, 형 비교표 v2 일치). 5건 이상 + fee>0 시 계산"},
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
        "disclaimer": "Track A 4 조건 자동 판정 — Track B (Harpoon) 추가 3 조건은 별도. 형 letter #11 기준.",
    }


@router.get("/day-direction")
def day_direction_status(request: Request):
    """[2026-05-21 부모] 오늘의 Day Direction 상태 — 매일 09:00 KST 결정.

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
        # [2026-05-23 부모] 9시 H4 일일 등락폭 기준선
        "h4_atr_pct": float(getattr(fm, "day_h4_atr_pct", 0.0)),
        "tp1_expected_pct": float(getattr(fm, "day_tp1_expected_pct", 0.0)),
        "tp2_expected_pct": float(getattr(fm, "day_tp2_expected_pct", 0.0)),
    }


@router.get("/h4-pa-snapshot")
def h4_pa_snapshot_status(request: Request):
    """[2026-05-21 부모] 최근 H4 PA Snapshot — 매 4시간 (1/5/9/13/17/21 KST) 코인 상태.

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
    """Phase K 최근 감지 기록 + 현재 시장 상태 (빈 카드 문구용).

    Returns:
      {
        "ok": True,
        "k_status": { enabled, paper_mode, btc_regime, btc_ema_gap_pct,
                      btc_regime_age_hours, adx_slope_check_enabled },
        "recent_detections": [...],  // coin별 최신 1건 dedupe
        "disclaimer": "실험적 신호 · 진입 권고 아님 · 1주 paper 후 정확도 공개",
      }
    """
    import os, json, time
    fm = _get_fm(request)
    cfg = fm.config
    now_ts = time.time()
    cutoff = now_ts - max(0.1, float(hours)) * 3600.0

    # k_status — 현재 상태 정보 (빈 카드 문구용)
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
        "min_conviction": float(getattr(cfg, "regime_transition_min_conviction", 80.0)),  # [2026-05-17 100점 ×10] 8→80, int→float
        "min_regime_age_min": float(getattr(cfg, "regime_transition_last_change_age_min", 180.0)),
        "adx_slope_check_enabled": bool(getattr(cfg, "adx_slope_check_enabled", True)),
    }

    # phase_k_paper_log.jsonl tail + dedupe
    log_path = os.path.join("runtime", "phase_k_paper_log.jsonl")
    detections_by_coin = {}  # market → latest entry
    count_today = {}         # market → count within window
    if os.path.exists(log_path):
        try:
            # 간단한 tail — 파일이 작다 가정 (24h 롤링 기록이라 MB 미만)
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
                    # 최신 1건 유지
                    if mkt not in detections_by_coin or ts > detections_by_coin[mkt].get("ts", 0):
                        detections_by_coin[mkt] = e
                    # 누적 카운트
                    count_today[mkt] = count_today.get(mkt, 0) + 1
        except Exception as exc:
            logger.debug("[Phase K] paper log read failed: %s", exc)

    # 정리 — 시간 역순
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
        "disclaimer": "실험적 신호 · 진입 권고 아님 · 1주 paper 후 정확도 공개",
    }
