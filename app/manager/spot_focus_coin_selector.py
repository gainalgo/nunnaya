# ============================================================
# Upbit FOCUS Coin Selector — Triple-Confirmation (long_only)
# ------------------------------------------------------------
# Source 1: Upbit KRW 마켓 거래대금 상위 (거래소별 — 새로 구현)
# Source 2: GreenPen multi-TF 분석  ← focus_coin_selector 재사용
# Source 3: 구조 필터               ← focus_coin_selector 재사용
#
# Source 2/3 가 client.get_kline() 만 의존하고, UpbitTradeClient 가
# Bybit 와 동일 포맷(oldest-first)으로 캔들을 주므로 그대로 상속한다.
# direction_mode 는 항상 "long_only" (현물).
# 0 candidates = "안 들어가는 것도 전략" (북극성)
# ============================================================
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# 유동성/변동 필터 (Upbit KRW 기준)
_MIN_TURNOVER_KRW = 5_000_000_000.0   # 24h 거래대금 50억 KRW 이상 (슬리피지 방지)
_MAX_CHANGE = 0.20                     # 24h 극변동 제외 (±20%)
_MIN_CHANGE = 0.005                    # 너무 잠잠하면 TP 안 맞음 (±0.5%)

# ── 거래소별 분기 (Bybit 현물=USDT) — Upbit/Bithumb(KRW) 경로는 100% 불변 ──
#   EXCHANGE_TYPE: BybitTradeClient="bybit" / Upbit·Bithumb=없음("") → 'KRW'.
_MIN_TURNOVER_USDT = 3_000_000.0       # 24h 거래대금 ~300만 USDT (≈50억 KRW 환산, Bybit/Binance 현물)


def _exchange_quote(client: Any) -> str:
    """client 견적통화 — 'USDT'(Bybit/Binance) 또는 'KRW'(Upbit/Bithumb)."""
    et = str(getattr(client, "EXCHANGE_TYPE", "")).lower()
    return "USDT" if et in ("bybit", "binance") else "KRW"


def _btc_ref(client: Any) -> str:
    """BTC 기준 심볼 (guard_score BTC 정렬용)."""
    return "BTCUSDT" if _exchange_quote(client) == "USDT" else "KRW-BTC"


def _accepts_market(client: Any, mk: Any) -> bool:
    """이 거래소의 견적통화 마켓인가 (KRW- 접두 vs USDT 접미)."""
    m = str(mk).upper()
    return m.endswith("USDT") if _exchange_quote(client) == "USDT" else m.startswith("KRW-")


def _min_turnover(client: Any) -> float:
    return _MIN_TURNOVER_USDT if _exchange_quote(client) == "USDT" else _MIN_TURNOVER_KRW


def _status_to_gate(status: str, passed: bool) -> str:
    """scanner status(동적 숫자/괄호 suffix 포함) → 안정적 게이트 키(GateLedger 집계용).
    같은 게이트가 매번 다른 숫자(점수/여유%)를 달고 와도 한 카운터로 합치도록 정규화한다.
    ★ 새 판단 0 — 이미 계산된 row["status"] 를 라벨로만 매핑(관측)."""
    if passed:
        return "PASS"
    s = str(status or "").strip()
    if not s:
        return "기타"
    # 대표 키워드(부분일치) → 안정 키. 동적 suffix(숫자/괄호)는 무시.
    table = (
        ("데이터부족", "데이터부족"),
        ("신호 없음", "신호없음"),
        ("역추세", "역추세차단"),
        ("SHORT", "SHORT제외"),
        ("약함", "약한신호"),
        ("구조", "구조탈락"),
        ("문턱", "conf문턱미달"),
        ("저항", "저항근접(headroom)"),
        ("점수", "점수미달(guard_score)"),
        ("ADX", "ADX미달"),
        ("투자유의", "투자유의차단"),
        ("주의환기", "주의환기차단"),
        ("오류", "스캔오류"),
    )
    for kw, gate in table:
        if kw in s:
            return gate
    return s[:24]


def _entry_score_threshold(cfg: Any, guard_score_threshold: float) -> float:
    """현물 최종 진입점수 문턱.

    Phase 2 배선 후 `guard_score_threshold` 는 이름은 유지하지만 의미는
    `base_conviction + guard_modifier` final score 문턱이다. 0 이면 기존처럼
    점수 게이트 OFF.
    """
    try:
        th = float(guard_score_threshold or 0.0)
    except (TypeError, ValueError):
        th = 0.0
    if th <= 0:
        return 0.0
    return th


def _final_entry_score_for(client: Any, market: str, cfg: Any,
                           btc_dir: str = "NEUTRAL") -> Optional[Dict[str, Any]]:
    """Phase 2 현물 점수: base conviction + guard-chain modifier.

    반환 필드는 scan/select/UI 가 같은 값을 보도록 공통으로 쓴다.
    실패 시 None(fail-open 가능 지점에서 호출부가 처리).
    """
    try:
        from app.manager.spot_conviction import compute_base_conviction
        from app.manager.spot_guard_chain import compute_entry_guard_score

        base, base_bd = compute_base_conviction(client, market, cfg, direction="LONG", btc_dir=btc_dir)
        mod, mod_bd = compute_entry_guard_score(client, market, cfg, direction="LONG", btc_dir=btc_dir)
        final = float(base) + float(mod)
        return {
            "base_conviction": round(float(base), 2),
            "guard_modifier": round(float(mod), 2),
            "final_score": round(final, 2),
            "conviction_score": round(final, 2),
            "base_breakdown": base_bd,
            "guard_breakdown": mod_bd,
        }
    except Exception as exc:
        logger.debug("[SPOT_SELECT] final score 계산 실패 %s: %s", market, exc)
        return None


def select_spot_focus_coin(
    system: Any,
    client: Any,
    *,
    primary_tf: str = "240",
    top_n: int = 10,
    min_conf: float = 0.4,
    exclude: Any = None,
    headroom_gate_pct: float = 0.0,
    overext_range_pos_pct: float = 0.0,
    overext_min_move_pct: float = 8.0,
    blowoff_move_pct: float = 0.0,
    guard_score_mode_enabled: bool = False,
    guard_score_threshold: float = 0.0,
    guard_score_total_cap: float = 0.0,
    block_warning: bool = False,
    block_caution: bool = False,
    record: Any = None,
) -> Optional[Dict]:
    """3중 확인 스캔 → 최고 final score LONG 후보 1개 (없으면 None).
    §② 게이트: headroom + overext + blowoff + final score(threshold>0일 때) 통과한 최상위 후보.
    block_warning/block_caution: 거래소 투자유의/주의환기 종목 진입 차단.
    record(market, gate, passed): 옵션 콜백 — 켜지면 스캔된 코인별 게이트 통과/거절을
      GateLedger 에 집계('왜 침묵했나' 관제판). None=집계 OFF(동작 0변화)."""
    rows = scan_spot_focus_candidates(
        system, client,
        primary_tf=primary_tf, top_n=top_n, min_conf=min_conf, exclude=exclude,
        headroom_gate_pct=headroom_gate_pct,
        guard_score_mode_enabled=guard_score_mode_enabled,
        guard_score_threshold=guard_score_threshold,
        guard_score_total_cap=guard_score_total_cap,
        block_warning=block_warning, block_caution=block_caution,
        record=record,
    )
    if not rows:
        logger.info("[SPOT_SELECT] no candidates from scan")
        return None

    # ★ §② 진입품질 게이트 — headroom(머리 위 저항 여유) + overext(24H 끝물 추격).
    #   통과하는 최상위 후보 선택. 게이트값<=0 이면 항상 통과(=종전 동작 0변화).
    from app.manager.spot_entry_quality import (
        check_headroom, check_overextension, check_blowoff,
    )
    # 24H 메트릭 — overext/blowoff 중 하나라도 켜졌을 때만, final 후보들만 1회 배치 fetch(중복 fetch 방지).
    metrics: Dict[str, Dict] = {}
    if overext_range_pos_pct > 0 or blowoff_move_pct > 0:
        metrics = _fetch_24h_metrics(client, [c.get("market") for c in rows if c.get("market")])
    best = None
    for c in rows:
        if not c.get("passed"):
            continue
        mk = c.get("market")
        price = float(c.get("price", 0) or 0)
        ok, reason = check_headroom(price, c.get("zones", []), min_headroom_pct=headroom_gate_pct)
        if not ok:
            logger.info("[SPOT_SELECT] %s %s — 건너뜀", mk, reason)
            continue
        m = metrics.get(mk) or {}
        if overext_range_pos_pct > 0:
            ok2, reason2 = check_overextension(
                m.get("last", price), m.get("hi", 0.0), m.get("lo", 0.0), m.get("move", 0.0),
                range_pos_pct=overext_range_pos_pct, min_move_pct=overext_min_move_pct,
            )
            if not ok2:
                logger.info("[SPOT_SELECT] %s %s — 건너뜀", mk, reason2)
                continue
        if blowoff_move_pct > 0:
            ok3, reason3 = check_blowoff(
                m.get("move", 0.0), blowoff_move_pct=blowoff_move_pct, direction="LONG",
            )
            if not ok3:
                logger.info("[SPOT_SELECT] %s %s — 건너뜀", mk, reason3)
                continue
        best = c
        break
    if best is None:
        logger.info("[SPOT_SELECT] 모든 후보 진입품질 게이트 차단(천장/끝물/파라볼릭) — skip")
        return None

    if best.get("confidence", 0) < min_conf:
        logger.info("[SPOT_SELECT] best %s conf=%.2f < min_conf %.2f — skip",
                    best.get("market"), best.get("confidence", 0), min_conf)
        return None

    # 현물 long_only 안전장치: SHORT 후보는 절대 통과 금지
    if best.get("direction") != "LONG":
        logger.info("[SPOT_SELECT] best %s dir=%s != LONG — blocked (spot long_only)",
                    best.get("market"), best.get("direction"))
        return None

    logger.info("[SPOT_SELECT] Selected: %s LONG (conf=%.2f final=%.1f base=%.1f mod=%.1f)",
                best.get("market"), best.get("confidence", 0),
                float(best.get("final_score", best.get("guard_score", 0)) or 0),
                float(best.get("base_conviction", 0) or 0),
                float(best.get("guard_modifier", 0) or 0))
    return best


def select_spot_contrarian_coin(
    system: Any,
    client: Any,
    *,
    top_n: int = 10,
    exclude: Any = None,
    coin_up_th: float = 3.0,
    coin_up_cap: float = 15.0,
    regime_gate: bool = True,
    block_warning: bool = False,
    block_caution: bool = False,
) -> Optional[Dict]:
    """역행(CONTRARIAN) 현물 후보 1개 — BTC 비추세(중립/하락)에서 *BTC 대비 상대강도* 코인.
    FOCUS(추세추종) 정반대 regime: 상승추세엔 OFF(regime_gate), 중립/하락에서만 = FOCUS churn 장.
    신호 = (코인 24h move − BTC 24h move) ≥ coin_up_th  ∧  move ≤ coin_up_cap(파라볼릭 펌프 제외).
    ★ v1 = 24h 상대강도 coarse proxy(기존 _fetch_24h_metrics 재사용, 새 캔들 fetch 0).
       paper 관측 후 단기(분봉) 상대강도로 정밀화 예정. long_only(현물).
    0 후보 = "안 들어가는 것도 전략"(북극성)."""
    # ── regime-gate: 상승추세는 FOCUS 영역 → 역행 OFF ──
    btc_dir = _btc_direction(client)
    if regime_gate and btc_dir == "UP":
        logger.info("[SPOT_CONTRA] BTC UPTREND — 역행 skip (FOCUS 영역)")
        return None
    markets = _source1_spot_volume(client, top_n=top_n, exclude=exclude,
                                   block_warning=block_warning, block_caution=block_caution)
    if not markets:
        logger.info("[SPOT_CONTRA] Source 1: no candidates from volume scan")
        return None
    btc_ref = _btc_ref(client)
    metrics = _fetch_24h_metrics(client, list(markets) + [btc_ref])
    if not metrics:
        logger.info("[SPOT_CONTRA] 24h 메트릭 없음 — skip (blind 진입 방지)")
        return None
    btc_move = float((metrics.get(btc_ref) or {}).get("move", 0.0))
    best = None
    for mk in markets:
        m = metrics.get(mk) or {}
        last = float(m.get("last", 0.0) or 0.0)
        move = float(m.get("move", 0.0) or 0.0)
        if last <= 0:
            continue
        rel = move - btc_move                      # BTC 대비 초과수익(상대강도)
        if rel < coin_up_th:                        # BTC보다 충분히 강해야 역행 진입 자격
            continue
        if coin_up_cap > 0 and move > coin_up_cap:  # 절대 24h 펌프 = exit유동성 함정 제외
            continue
        if best is None or rel > best["_rel"]:
            best = {"market": mk, "price": last, "move": move, "_rel": rel,
                    "direction": "LONG", "confidence": 0.0, "btc_dir": btc_dir,
                    "_source": "CONTRARIAN"}
    if best is None:
        logger.info("[SPOT_CONTRA] 역행 후보 0 (BTC=%s %.2f%%, 상대강도 ≥%.1f%% 없음)",
                    btc_dir, btc_move, coin_up_th)
        return None
    logger.info("[SPOT_CONTRA] Selected %s LONG move=%.2f%% rel=%.2f%% (btc=%s %.2f%%)",
                best["market"], best["move"], best["_rel"], btc_dir, btc_move)
    return best


def scan_spot_focus_candidates(
    system: Any,
    client: Any,
    *,
    primary_tf: str = "240",
    top_n: int = 10,
    min_conf: float = 0.4,
    exclude: Any = None,
    headroom_gate_pct: float = 0.0,
    guard_score_mode_enabled: bool = False,
    guard_score_threshold: float = 0.0,
    guard_score_total_cap: float = 0.0,
    block_warning: bool = False,
    block_caution: bool = False,
    record: Any = None,
) -> List[Dict]:
    """예비 후보 현황 — 거래대금 상위 N개를 GreenPen 으로 진단해 *전부* 반환(표시용).
    select 와 달리 차단된 것도 사유와 함께 보여준다 (Bybit GreenPen Scanner 미러).
    headroom_gate_pct>0 이면 천장 여유 부족 후보를 "천장 추격" 으로 표기(게이트 효과 미리보기).
    guard_score_mode_enabled 이면 Phase2 final score(base+modifier) 컬럼 표시, threshold>0 이면 미달 차단 표기.
    """
    from app.strategy.greenpen import full_analysis
    from app.strategy.greenpen.pa_detector import OHLCV
    from app.manager.focus_coin_selector import _source3_structural_filter

    # 표시용은 차단 안 하고 *전부* 보여줌(유의/주의 배지로 표기) — block 은 진입(select)에서만.
    markets = _source1_spot_volume(client, top_n=top_n, exclude=exclude)
    # 거래소 경고 플래그 (TTL 캐시 — 추가 fetch 거의 없음)
    try:
        warn = client.get_market_warnings()
    except Exception:
        warn = {}
    # BTC 정렬 — 스캔당 1회 (모든 후보 공통, 캐시)
    _btc_dir = _btc_direction(client) if guard_score_mode_enabled else "NEUTRAL"
    _cfg = getattr(system, "config", None)
    _score_threshold = _entry_score_threshold(_cfg, guard_score_threshold)
    rows: List[Dict] = []
    for market in markets:
        wf = warn.get(market, {}) if isinstance(warn, dict) else {}
        row = {
            "market": market, "price": 0.0, "direction": "—", "pa_pattern": None,
            "trend": "—", "confidence": 0.0, "status": "", "passed": False,
            "headroom_pct": None, "guard_score": None, "legacy_guard_score": None,
            "base_conviction": None, "guard_modifier": None, "final_score": None,
            "warning": bool(wf.get("warning")), "caution": bool(wf.get("caution")),
            "caution_kinds": wf.get("kinds", []),
        }
        try:
            raw = client.get_kline(market, interval=primary_tf, limit=31)
            if raw and len(raw) >= 2:
                raw = raw[:-1]  # Phase2: PA/추세 판단은 closed candle 기준.
            candles = []
            for r in raw:
                try:
                    candles.append(OHLCV(
                        open=float(r[1]), high=float(r[2]), low=float(r[3]),
                        close=float(r[4]), volume=float(r[5]) if len(r) > 5 else 0,
                        ts=float(r[0]) / 1000 if r[0] else 0,
                    ))
                except (IndexError, TypeError, ValueError):
                    continue
            if len(candles) < 15:
                row["status"] = "데이터부족"
                rows.append(row)
                continue
            gp = full_analysis(candles)
            trend = gp.structure.trend.value
            row["trend"] = trend
            row["price"] = candles[-1].close
            row["atr"] = gp.atr
            row["zones"] = _zser(gp.zones)
            # guard_score G1(ADX+추세conf) — 캔들 재사용(무fetch), 표시용
            if guard_score_mode_enabled:
                try:
                    from app.manager.spot_guard_score import compute_guard_score, gs_weights_from_config
                    _gpa = gp.pa_signals[0] if gp.pa_signals else None
                    gsc, _bd = compute_guard_score(
                        [c.high for c in candles], [c.low for c in candles],
                        [c.close for c in candles], gp.structure.confidence,
                        trend=trend,
                        pa_direction=(_gpa.direction.value if _gpa else ""),
                        pa_confidence=(float(_gpa.confidence) if _gpa else 0.0),
                        btc_direction=_btc_dir, price=row["price"],
                        zones=_zser(gp.zones), atr=gp.atr,
                        volumes=[c.volume for c in candles],
                        total_cap=guard_score_total_cap,
                        weights=gs_weights_from_config(getattr(system, "config", None)),
                    )
                    row["legacy_guard_score"] = gsc
                    row["guard_score"] = gsc   # 기본 표시 = legacy gs8(무fetch). final 은 LONG 확정 후 덮음.
                except Exception:
                    pass
            pa = gp.pa_signals[0] if gp.pa_signals else None
            if not pa:
                row["status"] = "신호 없음"
                rows.append(row)
                continue
            direction = pa.direction.value
            conf = float(pa.confidence)
            row["direction"] = direction
            row["pa_pattern"] = pa.pattern.value
            row["confidence"] = round(conf, 3)
            row["primary_sig"] = {
                "pattern": pa.pattern.value,
                "direction": direction,
                "confidence": conf,
                "atr": gp.atr,
            }
            # ★ final 진입점수(base conviction + guard modifier) — **LONG 신호 확정 후보만** 계산.
            #   (성능: 후보당 base+modifier=get_kline 30회/2초 → 신호없음/SHORT 까지 풀계산하면 top_n×30 폭주.
            #    실측 300회/스캔 → Scanner 멈춤·타임아웃. LONG 신호 있는 후보(보통 0~3개)만 = 폭주 해소.)
            if guard_score_mode_enabled and direction == "LONG":
                score_row = _final_entry_score_for(client, market, _cfg, btc_dir=_btc_dir)
                if score_row:
                    row.update(score_row)
                    row["guard_score"] = score_row["final_score"]  # 표시/게이트 = final(base+modifier)
            # headroom(머리 위 가장 가까운 RESISTANCE 여유 %) — 표시 + 게이트 미리보기
            overhead = [
                z.price_low for z in gp.zones
                if (z.type.value if hasattr(z.type, "value") else str(z.type)).upper() == "RESISTANCE"
                and z.price_low > row["price"]
            ]
            if overhead and row["price"] > 0:
                row["headroom_pct"] = round((min(overhead) - row["price"]) / row["price"] * 100.0, 2)
            # ADX (진입 adx_entry_gate 와 동일 기준) — candles 재사용(무fetch). 표시 + status 일치.
            _adxv = _adx_value(candles)
            row["adx"] = round(_adxv, 1) if _adxv is not None else None
            # 게이트(select 와 동일 판정) — 차단 사유 표기
            if direction == "LONG" and trend == "DOWNTREND":
                row["status"] = "역추세 차단"
            elif direction == "SHORT":
                row["status"] = "SHORT(현물 제외)"
            elif conf < 0.3:
                row["status"] = "약함(<0.3)"
            else:
                cand = {"market": market, "confidence": conf, "trend": trend}
                try:
                    s3 = _source3_structural_filter(client, cand, system)
                except Exception:
                    s3 = False
                if not s3:
                    row["status"] = "구조 탈락"
                elif conf < min_conf:
                    row["status"] = f"문턱 미달(<{min_conf:.2f})"
                elif (headroom_gate_pct > 0 and row["headroom_pct"] is not None
                        and row["headroom_pct"] < headroom_gate_pct):
                    # ★ headroom 게이트는 *머리 위 저항이 가까움*만 본다(코인이 끝물/레인지상단인지 아님).
                    #   → 레인지 중간 코인에 '천장 추격'은 과장. 정확히 '저항 근접'(여유=저항까지 거리).
                    row["status"] = f"저항 근접(여유 {row['headroom_pct']:.1f}%)"
                elif (_score_threshold > 0 and row["guard_score"] is not None
                        and row["guard_score"] < _score_threshold):
                    row["status"] = f"점수 미달({row['guard_score']:.0f}<{_score_threshold:.0f})"
                else:
                    # ★ [2026-06-19 부모] 실제 진입 게이트(adx_entry_gate: H1 ADX + 돌파면제)와 동일 판정으로 표시.
                    #   여기 도달 = 다른 게이트 다 통과한 finalist 뿐 → 호출 적음(fetch storm 아님). _adx_blocks_entry(H4) 대체.
                    try:
                        from app.manager.spot_guard_chain import adx_entry_gate as _adx_gate
                        _adx_ok, _adx_why = _adx_gate(client, market, _cfg)
                    except Exception:
                        _adx_ok, _adx_why = True, "adx_gate_err"
                    if not _adx_ok:
                        row["status"] = f"ADX 미달({_adx_why})"
                    else:
                        row["status"] = "✅ 진입 가능"
                        row["passed"] = True
        except Exception as exc:
            logger.warning("[UPBIT_SCAN] %s 진단 실패: %s", market, exc)
            row["status"] = "오류"
        # ★ 거래소 경고 — 최우선. 투자유의(block_warning)는 진입 차단으로 덮음, 주의는 표시만.
        if row["warning"] and block_warning:
            row["status"] = "⛔ 투자유의(차단)"
            row["passed"] = False
        elif row["caution"] and block_caution:
            row["status"] = "⛔ 주의환기(차단)"
            row["passed"] = False
        # ★ [2026-06-21] GateLedger 집계 — 이 코인이 어느 게이트에 걸렸나(관측만, 진입 불침).
        #   record 콜백 자체가 예외를 삼키지만 한 번 더 감싸 스캔 흐름을 절대 안 깬다.
        if record is not None:
            try:
                record(market, _status_to_gate(row.get("status", ""), bool(row.get("passed"))),
                       bool(row.get("passed")))
            except Exception:
                pass
        rows.append(row)

    # 진입가능 먼저, 그 다음 confidence 내림차순
    rows.sort(key=lambda x: (not x["passed"],
                             -float(x.get("guard_score") or 0),
                             -float(x.get("confidence") or 0)))
    return rows


def _parse_exclude(exclude: Any) -> set:
    """쉼표/리스트 입력을 정규화된 마켓 집합으로 (KRW- 접두 보정, 대문자)."""
    if not exclude:
        return set()
    items = exclude.split(",") if isinstance(exclude, str) else list(exclude)
    out = set()
    for it in items:
        s = str(it).strip().upper()
        if not s:
            continue
        if not s.startswith("KRW-"):
            s = "KRW-" + s
        out.add(s)
    return out


def _btc_direction(client: Any) -> str:
    """KRW-BTC 구조 추세 → 'UP'/'DOWN'/'NEUTRAL'. guard_score BTC 정렬용(스캔당 1회·캐시). 실패 NEUTRAL."""
    try:
        from app.strategy.greenpen.market_structure import analyze_structure
        from app.strategy.greenpen.pa_detector import OHLCV
        raw = client.get_kline(_btc_ref(client), interval="240", limit=30)
        candles = [OHLCV(open=float(r[1]), high=float(r[2]), low=float(r[3]), close=float(r[4]))
                   for r in raw if len(r) >= 5]
        if len(candles) < 15:
            return "NEUTRAL"
        t = analyze_structure(candles).trend.value
        return {"UPTREND": "UP", "DOWNTREND": "DOWN"}.get(t, "NEUTRAL")
    except Exception:
        return "NEUTRAL"


def _zser(zones) -> List[dict]:
    """GreenPen zone 객체 → guard_score Anchor 용 직렬화."""
    out = []
    for z in (zones or []):
        try:
            out.append({
                "type": z.type.value if hasattr(z.type, "value") else str(z.type),
                "price_low": z.price_low, "price_high": z.price_high,
            })
        except Exception:
            continue
    return out


def _adx_value(candles) -> Optional[float]:
    """candles(OHLCV, oldest-first) → primary ADX. 부족/실패 None.
    스캐너 표시·status 와 진입 adx_entry_gate 가 같은 기준 쓰도록(candles 재사용=무fetch)."""
    try:
        from app.strategy import indicators
        if not candles or len(candles) < 29:   # ADX(14) 최소 29봉
            return None
        a = indicators.adx([c.high for c in candles], [c.low for c in candles],
                           [c.close for c in candles])
        if not a:
            return None
        return float(a.get("adx", 0.0) or 0.0)
    except Exception:
        return None


def _adx_blocks_entry(system, adxv: Optional[float]) -> bool:
    """adx_filter_enabled(672 기본 True) 이고 adx < min_adx_entry(17) 면 진입 차단(=SIDEWAYS junk).
    진입 경로 spot_guard_chain.adx_entry_gate 와 동일 판정 — 스캐너 표시 일치용. adxv None=fail-open."""
    cfg = getattr(system, "config", None)
    if cfg is None or adxv is None or not getattr(cfg, "adx_filter_enabled", True):
        return False
    return adxv < float(getattr(cfg, "min_adx_entry", 17))


def _guard_score_for(client: Any, market: str, primary_tf: str, total_cap: float,
                     btc_dir: str = "NEUTRAL", weights: dict = None):
    """select 게이트용 guard_score. primary kline(캐시) + full_analysis(structure+PA) → 점수. 실패 None."""
    try:
        from app.strategy.greenpen import full_analysis
        from app.strategy.greenpen.pa_detector import OHLCV
        from app.manager.spot_guard_score import compute_guard_score
        raw = client.get_kline(market, interval=primary_tf, limit=30)
        candles = [OHLCV(open=float(r[1]), high=float(r[2]), low=float(r[3]), close=float(r[4]),
                         volume=float(r[5]) if len(r) > 5 else 0)
                   for r in raw if len(r) >= 5]
        if len(candles) < 15:
            return None
        gp = full_analysis(candles)
        _pa = gp.pa_signals[0] if gp.pa_signals else None
        gsc, _ = compute_guard_score(
            [c.high for c in candles], [c.low for c in candles], [c.close for c in candles],
            gp.structure.confidence, trend=gp.structure.trend.value,
            pa_direction=(_pa.direction.value if _pa else ""),
            pa_confidence=(float(_pa.confidence) if _pa else 0.0),
            btc_direction=btc_dir, price=candles[-1].close,
            zones=_zser(gp.zones), atr=gp.atr,
            volumes=[c.volume for c in candles], total_cap=total_cap,
            weights=weights,
        )
        return gsc
    except Exception as exc:
        logger.debug("[SPOT_SELECT] guard_score 계산 실패 %s: %s", market, exc)
        return None


def score_timeline(client: Any, market: str, primary_tf: str = "240",
                   count: int = 60, total_cap: float = 80.0,
                   weights: dict = None) -> List[Dict]:
    """과거 시점별 guard_score + conf 궤적 (점수↔차트 정합 검증용).
    각 시점에서 라이브 스캔과 동일하게 trailing 30캔들 full_analysis + guard_score 재계산.
    BTC 방향도 그 시점 기준(시점별)으로 반영. 반환 = 오래된→최신 순 rows."""
    try:
        from app.strategy.greenpen import full_analysis
        from app.strategy.greenpen.pa_detector import OHLCV
        from app.strategy.greenpen.market_structure import analyze_structure
        from app.manager.spot_guard_score import compute_guard_score
    except Exception as exc:
        logger.warning("[SPOT_SELECT] score_timeline import 실패: %s", exc)
        return []

    def _mk(raw):
        cs, ts = [], []
        for r in raw:
            if len(r) >= 5:
                cs.append(OHLCV(open=float(r[1]), high=float(r[2]), low=float(r[3]),
                                close=float(r[4]), volume=float(r[5]) if len(r) > 5 else 0))
                ts.append(int(r[0]))
        return cs, ts

    try:
        need = int(count) + 30
        wc, wts = _mk(client.get_kline(market, interval=primary_tf, limit=need))
        if len(wc) < 31:
            return []
        bc, bts = _mk(client.get_kline(_btc_ref(client), interval=primary_tf, limit=need))

        def btc_dir_at(t):
            idx = [i for i, x in enumerate(bts) if x <= t]
            if not idx:
                return "NEUTRAL"
            j = idx[-1]
            win = bc[max(0, j - 29):j + 1]
            if len(win) < 15:
                return "NEUTRAL"
            try:
                s = analyze_structure(win)
                return {"UPTREND": "UP", "DOWNTREND": "DOWN"}.get(s.trend.value, "NEUTRAL")
            except Exception:
                return "NEUTRAL"

        rows: List[Dict] = []
        for i in range(30, len(wc)):
            win = wc[i - 30:i]
            try:
                gp = full_analysis(win)
                pa = gp.pa_signals[0] if gp.pa_signals else None
                conf = float(pa.confidence) if pa else 0.0
                gsc, _ = compute_guard_score(
                    [c.high for c in win], [c.low for c in win], [c.close for c in win],
                    gp.structure.confidence, trend=gp.structure.trend.value,
                    pa_direction=(pa.direction.value if pa else ""),
                    pa_confidence=conf, btc_direction=btc_dir_at(wts[i - 1]),
                    price=win[-1].close, zones=_zser(gp.zones), atr=gp.atr,
                    volumes=[c.volume for c in win], total_cap=total_cap,
                    weights=weights,
                )
                rows.append({
                    "ts": wts[i - 1], "close": round(win[-1].close, 8),
                    "trend": gp.structure.trend.value, "conf": round(conf, 3),
                    "guard_score": gsc, "pa": (pa.pattern.value if pa else None),
                })
            except Exception:
                continue
        return rows
    except Exception as exc:
        logger.warning("[SPOT_SELECT] score_timeline %s 실패: %s", market, exc)
        return []


def _fetch_24h_metrics(client: Any, markets: List[str]) -> Dict[str, Dict]:
    """final 후보들의 24H 메트릭(last/hi/lo/move%) 한 번에 배치 조회 — overext 게이트용.
    실패/누락은 빈 dict → 게이트는 no_data 로 통과(fail-open)."""
    out: Dict[str, Dict] = {}
    codes = [m for m in (markets or []) if m]
    if not codes:
        return out
    try:
        tickers = client.get_tickers(codes)
    except Exception as exc:
        logger.warning("[SPOT_SELECT] 24H 메트릭 조회 실패(overext skip): %s", exc)
        return out
    for t in (tickers or []):
        if not isinstance(t, dict):
            continue
        mk = str(t.get("market", ""))
        if not mk:
            continue
        out[mk] = {
            "last": float(t.get("trade_price", 0) or 0),
            "hi": float(t.get("high_price", 0) or 0),
            "lo": float(t.get("low_price", 0) or 0),
            "move": float(t.get("signed_change_rate", 0) or 0) * 100.0,
        }
    return out


def _source1_spot_volume(client: Any, top_n: int = 10, exclude: Any = None,
                          block_warning: bool = False, block_caution: bool = False) -> List[str]:
    """Upbit KRW 마켓을 24h 거래대금 기준 정렬해 상위 N개. exclude=운영 제외 마켓.
    block_warning=True 면 거래소 투자유의 종목(상폐위험) 제외, block_caution=True 면 주의환기도 제외.
    경고 플래그는 get_all_markets(isDetails=true) 응답에서 추출 — 추가 fetch 없음."""
    skip = _parse_exclude(exclude)
    try:
        markets = client.get_all_markets()
        codes = []
        for m in markets:
            mk = m.get("market")
            if not _accepts_market(client, mk) or mk in skip:
                continue
            if block_warning or block_caution:
                try:
                    f = client._parse_market_flags(m)
                    if block_warning and f.get("warning"):
                        logger.info("[SPOT_SELECT] %s 투자유의 종목 — 진입 차단", mk)
                        continue
                    if block_caution and f.get("caution"):
                        logger.info("[SPOT_SELECT] %s 주의환기(%s) — 진입 차단",
                                    mk, ",".join(f.get("kinds", [])))
                        continue
                except Exception:
                    pass
            codes.append(mk)
        if not codes:
            return []

        tickers: List[Dict] = []
        for i in range(0, len(codes), 100):  # URL 길이 방지 배치
            try:
                tickers.extend(client.get_tickers(codes[i:i + 100]))
            except Exception as exc:
                logger.warning("[SPOT_SELECT] ticker batch %d failed: %s", i, exc)

        scored = []
        for t in tickers:
            if not isinstance(t, dict):
                continue
            market = str(t.get("market", ""))
            turnover = float(t.get("acc_trade_price_24h", 0) or 0)   # KRW(Upbit) / USDT(Bybit)
            change = float(t.get("signed_change_rate", 0) or 0)
            if turnover < _min_turnover(client):
                continue
            if abs(change) > _MAX_CHANGE:
                continue
            if abs(change) < _MIN_CHANGE:
                continue
            scored.append((market, turnover, abs(change)))

        scored.sort(key=lambda x: (-x[1], -x[2]))
        return [s[0] for s in scored[:top_n]]
    except Exception as exc:
        logger.warning("[SPOT_SELECT] Source 1 volume scan error: %s", exc)
        return []
