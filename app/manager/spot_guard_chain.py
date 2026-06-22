# ============================================================
# Spot Guard Chain — 선물 FOCUS _compute_guard_score_modifiers 충실 *통째* 이식 (superset)
# ------------------------------------------------------------
# 부모 지시: "선물 진입이 잘 고르는 건 검증 끝난 머신 — 하나씩 검증 말고 통째 복사해
#            현물에 꽂아라." 현물은 SIDEWAYS junk 진입으로 손실 中.
#
# 원본: app/manager/focus_manager.py:2306-2688 (_compute_guard_score_modifiers,
#       ★라이브 선물 보물 = READ-ONLY) + 13266~/7417~/14354~ ADX 진입 게이트.
#   ─ 본 모듈은 선물 24요소 *충실판(superset)* — spot_guard_score.py 8요소와 중복되어도 OK.
#   ─ 순수/모듈 함수: self 상태 0. import = app.strategy.greenpen / app.strategy.indicators /
#     client.get_kline 만. 선물 전용 메서드(_linear_last_price·day_direction·peer_brief 등) 절대 호출 X.
#
# 포팅 정책 (분석 스펙 spot_gap / guard_score 기준):
#   portable  = 순수 kline → spot client.get_kline 그대로.
#   adapt     = _linear_last_price → 마지막 close / day_direction → btc_dir 인자 / 24h ticker → kline 일봉.
#   skip(0점) = 현물 인프라 없는 것(Flow Reversal·Alt-BTC·Peer 5종·Day Box 상태머신) = 안전 0점.
#
# ★ 현물 long_only — direction 기본 "LONG". 모든 SHORT 분기는 보존(무해)하되 호출은 LONG.
# ★ 가중치는 전부 SpotGazuaConfig 의 guard_score_* (672) 필드를 getattr 로 읽음(미주입 시 선물 기본값).
# ============================================================
from __future__ import annotations

import datetime
import logging
from typing import Any, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ── 캔들 유틸 (get_kline = oldest-first [ts,open,high,low,close,vol,turnover]) ──
#   ※ 선물·현물 get_kline 모두 oldest-first (bybit_trade.py:554 · upbit_trade.py:328 둘 다 reversed 정렬,
#     focus_manager.py:2778 부모 검증 주석 'oldest first → raw[-1]=최신봉'). 따라서 raw[-1]=forming(진행봉).
#   ★ 단, 선물 _check_pa_completion(focus_manager.py:3088) 의 `reversed(raw) # oldest first` 는 이미 oldest-first
#     를 한 번 더 뒤집어 newest-first 로 만들어 forming 대신 *가장 오래된 봉*을 빼는 잠복버그(주석이 사실과 반대).
#     현물은 docstring 의도대로(huikkang=가장 최근 마감봉=closed[-1], forming=ordered[-1] 제외) reverse 생략 = 정답.
def _kl(client: Any, market: str, interval: str, limit: int) -> List[list]:
    try:
        raw = client.get_kline(market, interval=interval, limit=limit)
        return raw or []
    except Exception as exc:
        logger.debug("[SPOT_CHAIN] get_kline fail %s %s: %s", market, interval, exc)
        return []


def _ohlcv(raw: List[list]):
    """raw kline → greenpen OHLCV 리스트(oldest-first 그대로)."""
    from app.strategy.greenpen.pa_detector import OHLCV
    out = []
    for r in raw:
        if len(r) >= 5:
            out.append(OHLCV(open=float(r[1]), high=float(r[2]),
                             low=float(r[3]), close=float(r[4]),
                             volume=float(r[5]) if len(r) > 5 else 0.0))
    return out


def _last_price(client: Any, market: str, raw_primary: Optional[List[list]] = None) -> float:
    """adapt: 선물 _linear_last_price 대체 — client.last_price 있으면 그것, 없으면 마지막 close.
    순수 — 선물 전용 메서드 호출 안 함."""
    try:
        lp = getattr(client, "last_price", None)
        if callable(lp):
            v = lp(market)
            if v and float(v) > 0:
                return float(v)
    except Exception:
        pass
    try:
        if raw_primary and len(raw_primary[-1]) >= 5:
            return float(raw_primary[-1][4])
        raw = _kl(client, market, "5", 2)
        if raw and len(raw[-1]) >= 5:
            return float(raw[-1][4])
    except Exception:
        pass
    return 0.0


def _atr_from(highs: List[float], lows: List[float], closes: List[float], period: int = 14) -> float:
    """인라인 ATR(true range) — 선물 원본의 깨진 indicators.atr import 대신 직접 계산."""
    trs, pc = [], None
    for h, l, c in zip(highs, lows, closes):
        tr = (h - l) if pc is None else max(h - l, abs(h - pc), abs(l - pc))
        trs.append(tr)
        pc = c
    if not trs:
        return 0.0
    return sum(trs[-period:]) / min(len(trs), period)


# ============================================================
# PA Completion — 선물 _check_pa_completion (focus_manager.py:3069-3134) 순수 이식
#   ① PA(H4 OR H1) / ①-2 D1 / ③ H1 PA 펄스가 공유. spot=oldest-first → reverse 안 함.
# ============================================================
def _pa_completion(client: Any, market: str, direction: str, tf: str, cfg: Any) -> bool:
    """Sig + 후속봉(뒤꼬리) 둘 다 마감 = PA 완성. 원본 focus_manager.py:3069-3134.
    에러/부족 시 False(fail-closed, 안전)."""
    try:
        min_ratio = float(getattr(cfg, "pa_completion_huikkang_min_ratio", 1.5))
        lookback = max(2, int(getattr(cfg, "pa_completion_lookback_bars", 3)))
        sig_max = float(getattr(cfg, "pa_completion_sig_max_ratio", 1.0))
        need = lookback + 3
        raw = _kl(client, market, tf, max(need + 2, 8))
        if not raw or len(raw) < need:
            return False
        ordered = list(raw)                # ★ 현물 oldest-first 그대로 (선물처럼 reverse X)
        closed = ordered[:-1]              # forming(마지막) 제외 → 마감 캔들만
        if len(closed) < lookback + 2:
            return False
        huikkang = closed[-1]
        h_open, h_close = float(huikkang[1]), float(huikkang[4])
        h_body = abs(h_close - h_open)
        h_dir = "UP" if h_close > h_open else "DOWN"
        want_dir = "UP" if (direction or "").upper() == "LONG" else "DOWN"
        if h_dir != want_dir:
            return False
        prev_bodies = []
        for c in closed[-(lookback + 1):-1]:
            try:
                pb = abs(float(c[4]) - float(c[1]))
                if pb > 0:
                    prev_bodies.append(pb)
            except Exception:
                continue
        if not prev_bodies:
            return False
        avg_prev = sum(prev_bodies) / len(prev_bodies)
        if h_body < avg_prev * min_ratio:
            return False
        sig = closed[-2]
        s_open, s_close = float(sig[1]), float(sig[4])
        s_body = abs(s_close - s_open)
        s_dir = "UP" if s_close > s_open else "DOWN"
        if s_dir == h_dir:
            return False
        if s_body > h_body * sig_max:
            return False
        return True
    except Exception as exc:
        logger.debug("[SPOT_CHAIN] _pa_completion %s %s tf=%s: %s", market, direction, tf, exc)
        return False


def _in_h4_pulse_window(cfg: Any) -> bool:
    """② 선물 _in_h4_pulse_window (focus_manager.py:3170-3191) — 시계만(데이터 무관).
    KST 01/05/09/13/17/21시 마감 후 window_min 분 안. 에러 시 True(안전 통과)."""
    try:
        w = max(1, int(getattr(cfg, "h4_pulse_window_min", 30)))
        dt = datetime.datetime.now()       # local KST 가정
        if dt.hour not in (1, 5, 9, 13, 17, 21):
            return False
        if dt.minute >= w:
            return False
        return True
    except Exception:
        return True


def _structure_trend(client: Any, market: str, tf: str = "240", limit: int = 30) -> str:
    """⑬/④ analyze_structure trend.value (UPTREND/DOWNTREND/SIDEWAYS). 실패 ''."""
    try:
        from app.strategy.greenpen.market_structure import analyze_structure
        candles = _ohlcv(_kl(client, market, tf, limit))
        if len(candles) < 20:
            return ""
        st = analyze_structure(candles)
        return (st.trend.value if hasattr(st.trend, "value") else str(st.trend)).upper()
    except Exception as exc:
        logger.debug("[SPOT_CHAIN] _structure_trend %s: %s", market, exc)
        return ""


def _fetch_24h_metrics(client: Any, market: str) -> Optional[dict]:
    """㉑㉔ adapt: 선물 _get_24h_ticker(Bybit linear) 대체 — 일봉('D') hi/lo/chg 로 재구성.
    {last, high, low, move_pct, range_pos_pct}. 실패 None."""
    try:
        raw = _kl(client, market, "D", 2)
        if not raw or len(raw[-1]) < 5:
            return None
        day = raw[-1]
        o, h, l, c = float(day[1]), float(day[2]), float(day[3]), float(day[4])
        if c <= 0 or h <= l:
            return None
        move_pct = (c - o) / o * 100.0 if o > 0 else 0.0
        range_pos = (c - l) / (h - l) * 100.0
        return {"last": c, "high": h, "low": l,
                "move_pct": move_pct, "range_pos_pct": range_pos}
    except Exception as exc:
        logger.debug("[SPOT_CHAIN] _fetch_24h_metrics %s: %s", market, exc)
        return None


# ============================================================
# compute_entry_guard_score — 선물 _compute_guard_score_modifiers 충실 포팅
# ============================================================
def compute_entry_guard_score(
    client: Any,
    market: str,
    cfg: Any,
    direction: str = "LONG",
    btc_dir: str = "NEUTRAL",
) -> Tuple[float, List[str]]:
    """선물 가드 24요소를 현물용으로 통째 이식한 guard_score (base_conv 미포함, 가산/감점 합).

    원본 app/manager/focus_manager.py:2306-2688 (_compute_guard_score_modifiers).
    Returns (total_modifier: float, breakdown: list[str]).
      ─ portable 컴포넌트 = 선물 그대로(순수 kline).
      ─ adapt = _linear_last_price → 마지막 close / day_direction → btc_dir 인자 / 24h ticker → 일봉.
      ─ skip(0점) = Flow Reversal⑮ · Alt-BTC⑯ · Peer 5종⑰⑱⑲⑳㉕ · Day Box⑥ (현물 인프라 없음, 안전 0).
    각 컴포넌트는 try/except 로 독립(한 컴포넌트 실패가 전체를 막지 않음 = 선물 동일).

    ★ 미배선(dead code) — 아직 어디서도 호출 안 함. ✅ 적대적 감사 4건 *수정 완료*(선물 verbatim):
        · ㉑ Overext — ADX-exempt(돌파 면제) 추가 + range_pos 0~1 스케일 교정
        · ㉔ Blowoff — 곱셈→선형보간(선물 _check_blowoff 동일)
        · ㉒ Inflection — slope%·EMA-MACD lean·방향게이팅 verbatim
        · ㉓ Retest — _pivots·post극값·q소프트·FAIL이탈 verbatim
      ⚠️ 배선(스캐너 점수로 사용) 시: compute_entry_guard_score 는 conviction *modifier*(±, base 미포함)라
      스캐너 standalone threshold(50)에 그대로 못 끼움 — base conviction 모델과 합산 필요(별도 설계).
    """
    d = (direction or "LONG").upper()
    bdir = (btc_dir or "NEUTRAL").upper()
    total = 0.0
    bd: List[str] = []
    # 추세정렬 multicollinearity 캡용 포착 (선물 2317-2320)
    _ra_frame = 0.0
    _ra_trend = 0.0
    _ra_altbtc = 0.0       # 현물 skip → 0 유지(자동 무해)
    _ra_btcalign = 0.0

    # ── ① PA Completion (H4 OR H1) — 부모님 핵심 (선물 2322-2336) ──
    try:
        _h4 = _pa_completion(client, market, d, "240", cfg)
        _h1 = _pa_completion(client, market, d, "60", cfg) if not _h4 else False
        if _h4 or _h1:
            s = float(getattr(cfg, "guard_score_pa_completion_ok", 30.0))
            total += s; bd.append(f"PA{s:+.0f}({'H4' if _h4 else 'H1'})")
        else:
            s = float(getattr(cfg, "guard_score_pa_completion_none", -10.0))
            total += s; bd.append(f"PA{s:+.0f}")
    except Exception as exc:
        logger.debug("[SPOT_CHAIN] PA eval: %s", exc)

    # ── ①-2 D1 (일봉) PA — 큰 그림 방향 (선물 2337-2349) ──
    #   ★ 거래소별 'D' interval 지원 다름 → 미지원 시 _pa_completion 이 부족으로 False(none 감점).
    try:
        _d1 = _pa_completion(client, market, d, "D", cfg)
        if _d1:
            s = float(getattr(cfg, "guard_score_d1_pa_ok", 25.0))
            total += s; bd.append(f"D1PA{s:+.0f}")
        else:
            s = float(getattr(cfg, "guard_score_d1_pa_none", -5.0))
            total += s; bd.append(f"D1PA{s:+.0f}")
    except Exception as exc:
        logger.debug("[SPOT_CHAIN] D1 PA eval: %s", exc)

    # ── ② H4 Pulse (60분 창) — 시계만 portable (선물 2350-2358). PreClose 는 현물 skip ──
    try:
        if _in_h4_pulse_window(cfg):
            s = float(getattr(cfg, "guard_score_h4_pulse_in", 20.0))
            total += s; bd.append(f"H4Pulse{s:+.0f}")
        else:
            s = float(getattr(cfg, "guard_score_h4_pulse_out", -3.0))
            total += s; bd.append(f"H4Pulse{s:+.0f}")
    except Exception as exc:
        logger.debug("[SPOT_CHAIN] H4 pulse: %s", exc)

    # ── ③ H1 PA Pulse (선물 2374-2384) — adapt: day_direction 의존 제거(현물 없음),
    #   순수 H1 PA 완성만 인식. h1_pa_pulse_require_day_dir 는 무시(스펙: False 면 portable). ──
    try:
        _h1pulse = _pa_completion(client, market, d, "60", cfg)
        if _h1pulse:
            s = float(getattr(cfg, "guard_score_h1_pa_in", 15.0))
            total += s; bd.append(f"H1PA{s:+.0f}")
        else:
            s = float(getattr(cfg, "guard_score_h1_pa_out", -2.0))
            total += s; bd.append(f"H1PA{s:+.0f}")
    except Exception as exc:
        logger.debug("[SPOT_CHAIN] H1 PA: %s", exc)

    # ── ④ Frame Guard (24h 레인지 정렬) — portable (선물 2385-2399) ──
    #   현물 long_only: UPTREND=정렬(+) / SIDEWAYS=중립 / DOWNTREND=역행(-). _ra_frame 포착.
    try:
        ftrend = _structure_trend(client, market, "240", 30)
        if d == "LONG":
            if ftrend == "UPTREND":
                s = float(getattr(cfg, "guard_score_frame_aligned", 15.0))
                total += s; bd.append(f"Frame{s:+.0f}(정렬)"); _ra_frame = s
            elif ftrend == "DOWNTREND":
                s = float(getattr(cfg, "guard_score_frame_opposite", -20.0))
                total += s; bd.append(f"Frame{s:+.0f}(역행)"); _ra_frame = s
            elif ftrend == "SIDEWAYS":
                s = float(getattr(cfg, "guard_score_frame_neutral", 5.0))
                total += s; bd.append(f"Frame{s:+.0f}(중립)"); _ra_frame = s
        else:  # SHORT 분기 보존(현물 미사용·무해)
            if ftrend == "DOWNTREND":
                s = float(getattr(cfg, "guard_score_frame_aligned", 15.0))
                total += s; bd.append(f"Frame{s:+.0f}(정렬)"); _ra_frame = s
            elif ftrend == "UPTREND":
                s = float(getattr(cfg, "guard_score_frame_opposite", -20.0))
                total += s; bd.append(f"Frame{s:+.0f}(역행)"); _ra_frame = s
            elif ftrend == "SIDEWAYS":
                s = float(getattr(cfg, "guard_score_frame_neutral", 5.0))
                total += s; bd.append(f"Frame{s:+.0f}(중립)"); _ra_frame = s
    except Exception as exc:
        logger.debug("[SPOT_CHAIN] Frame: %s", exc)

    # ── ⑤ Anchor Fast-Track (눌림목 근접) — adapt: _linear_last_price → 마지막 close (선물 2400-2411) ──
    #   현물식: 가장 가까운 SUPPORT(LONG) 에 ATR 근접 = 사이클 시작점. zones 는 greenpen.full_analysis.
    try:
        raw_p = _kl(client, market, str(getattr(cfg, "primary_tf", "240")), 30)
        candles = _ohlcv(raw_p)
        price = _last_price(client, market, raw_p)
        if len(candles) >= 15 and price > 0:
            from app.strategy.greenpen import full_analysis
            gp = full_analysis(candles)
            atr = float(getattr(gp, "atr", 0.0) or 0.0)
            zones = getattr(gp, "zones", None) or []
            max_prox = float(getattr(cfg, "anchor_fasttrack_max_proximity", 0.33))
            if atr > 0:
                if d == "LONG":
                    levels = [float(getattr(z, "price_high", 0) or 0) for z in zones
                              if str(getattr(getattr(z, "type", None), "value", getattr(z, "type", ""))).upper() == "SUPPORT"
                              and float(getattr(z, "price_high", 0) or 0) <= price]
                    nearest = max(levels) if levels else None
                else:
                    levels = [float(getattr(z, "price_low", 0) or 0) for z in zones
                              if str(getattr(getattr(z, "type", None), "value", getattr(z, "type", ""))).upper() == "RESISTANCE"
                              and float(getattr(z, "price_low", 0) or 0) >= price]
                    nearest = min(levels) if levels else None
                if nearest is not None:
                    prox = abs(price - nearest) / atr
                    if prox <= max_prox:
                        s = float(getattr(cfg, "guard_score_anchor_close", 20.0))
                        total += s; bd.append(f"FT{s:+.0f}(prox{prox:.2f})")
                    elif prox > 1.0:
                        s = float(getattr(cfg, "guard_score_anchor_far", -10.0))
                        total += s; bd.append(f"FT{s:+.0f}(prox{prox:.2f})")
                    # 0.33~1.0 = 0점
    except Exception as exc:
        logger.debug("[SPOT_CHAIN] Anchor: %s", exc)

    # ── ⑥ Day Box Guard — SKIP(0점) ──
    #   day_box_state(09시 박스 형성·lock) 상태머신 = 비순수·시계 의존, 현물 매니저에 없음.
    #   스펙 권장 skip. overext(㉑) 가 24h 범위 상단 추종을 대체 커버.

    # ── ⑦ microtiming 5M (RSI/MACD/BB 변곡) — portable (선물 2426-2437) ──
    try:
        if getattr(cfg, "microtiming_5m_enabled", False):
            _mt_ok = _microtiming_5m_ok(client, market, d, cfg)
            if _mt_ok:
                s = float(getattr(cfg, "guard_score_microtiming_ok", 10.0))
                total += s; bd.append(f"MT{s:+.0f}")
            else:
                s = float(getattr(cfg, "guard_score_microtiming_no", -5.0))
                total += s; bd.append(f"MT{s:+.0f}")
    except Exception as exc:
        logger.debug("[SPOT_CHAIN] microtiming: %s", exc)

    # ── ⑧ raw_body_guard (3봉 net 방향) — portable (선물 2438-2449) ──
    try:
        if getattr(cfg, "raw_body_guard_enabled", getattr(cfg, "raw_body_enabled", False)):
            _rb_against = _raw_body_against(client, market, d, cfg)
            if _rb_against:
                s = float(getattr(cfg, "guard_score_raw_body_against", -8.0))
                total += s; bd.append(f"RawBody{s:+.0f}")
            else:
                s = float(getattr(cfg, "guard_score_raw_body_align", 5.0))
                total += s; bd.append(f"RawBody{s:+.0f}")
    except Exception as exc:
        logger.debug("[SPOT_CHAIN] raw_body: %s", exc)

    # ── ⑨ momentum_deriv (RSI/MACD 변화율) — portable (선물 2450-2461) ──
    try:
        if getattr(cfg, "momentum_deriv_guard_enabled", getattr(cfg, "momentum_deriv_enabled", False)):
            _md_against = _momentum_deriv_against(client, market, d, cfg)
            if _md_against:
                s = float(getattr(cfg, "guard_score_momentum_deriv_against", -5.0))
                total += s; bd.append(f"MomDeriv{s:+.0f}")
            else:
                s = float(getattr(cfg, "guard_score_momentum_deriv_align", 5.0))
                total += s; bd.append(f"MomDeriv{s:+.0f}")
    except Exception as exc:
        logger.debug("[SPOT_CHAIN] momentum_deriv: %s", exc)

    # ── ⑩ BTC 방향 정렬 — adapt: self.day_direction → btc_dir 인자 (선물 2462-2476) ──
    #   현물엔 BTC breadth 인프라 없음 → 호출측이 selector._btc_direction(BTC H4 구조)로 주입.
    try:
        if bdir in ("UP", "DOWN") and d in ("LONG", "SHORT"):
            want = "UP" if d == "LONG" else "DOWN"
            if bdir == want:
                s = float(getattr(cfg, "guard_score_btc_aligned", 15.0))
                total += s; bd.append(f"BTC{s:+.0f}(정렬)"); _ra_btcalign = s
            else:
                s = float(getattr(cfg, "guard_score_btc_opposite", -15.0))
                total += s; bd.append(f"BTC{s:+.0f}(역행)"); _ra_btcalign = s
        # NEUTRAL = 0
    except Exception as exc:
        logger.debug("[SPOT_CHAIN] BTC align: %s", exc)

    # ── ⑪ ADX 강/약 — portable (선물 2477-2504). SIDEWAYS-ADX 가점면제 보정 동일 ──
    try:
        from app.strategy import indicators
        raw_adx = _kl(client, market, str(getattr(cfg, "primary_tf", "240")), 60)
        highs = [float(r[2]) for r in raw_adx if len(r) >= 5]
        lows = [float(r[3]) for r in raw_adx if len(r) >= 5]
        closes = [float(r[4]) for r in raw_adx if len(r) >= 5]
        a = indicators.adx(highs, lows, closes)
        adx = float(a.get("adx", 0.0)) if a else 0.0
        if adx >= 30:
            _flat = False
            if getattr(cfg, "guard_score_adx_strong_requires_trend", False):
                if _structure_trend(client, market, "240", 30) == "SIDEWAYS":
                    _flat = True
            if _flat:
                bd.append(f"ADX·0({adx:.0f}≥30,횡보면제)")
            else:
                s = float(getattr(cfg, "guard_score_adx_strong", 10.0))
                total += s; bd.append(f"ADX{s:+.0f}({adx:.0f}≥30)")
        elif adx < 20:
            s = float(getattr(cfg, "guard_score_adx_weak", -5.0))
            total += s; bd.append(f"ADX{s:+.0f}({adx:.0f}<20)")
    except Exception as exc:
        logger.debug("[SPOT_CHAIN] ADX: %s", exc)

    # ── ⑫ 거래량 big + 방향 일치 (5M) — portable (선물 2505-2522). vol index r[5] ──
    try:
        raw5 = _kl(client, market, "5", 20)
        if raw5 and len(raw5) >= 15:
            vols = [float(r[5]) if len(r) > 5 else 0.0 for r in raw5[-15:]]
            cur_vol = vols[-1] if vols else 0.0
            avg_vol = (sum(vols[:-1]) / max(len(vols) - 1, 1)) if len(vols) > 1 else 1.0
            last = raw5[-1]
            last_open = float(last[1]) if len(last) > 1 else 0.0
            last_close = float(last[4]) if len(last) > 4 else 0.0
            last_dir = "UP" if last_close > last_open else "DOWN"
            want_vol_dir = "UP" if d == "LONG" else "DOWN"
            if avg_vol > 0 and cur_vol >= avg_vol * 2.0 and last_dir == want_vol_dir:
                s = float(getattr(cfg, "guard_score_vol_big_align", 10.0))
                total += s; bd.append(f"Vol{s:+.0f}(big {cur_vol/avg_vol:.1f}x)")
    except Exception as exc:
        logger.debug("[SPOT_CHAIN] Vol: %s", exc)

    # ── ⑬ H4 trend confidence (analyze_structure) — portable (선물 2523-2547). _ra_trend 포착 ──
    try:
        trend_str = _structure_trend(client, market, "240", 30)
        if trend_str in ("UPTREND", "DOWNTREND"):
            if (trend_str == "UPTREND" and d == "LONG") or (trend_str == "DOWNTREND" and d == "SHORT"):
                s = float(getattr(cfg, "guard_score_trend_high_conf", 10.0))
                total += s; bd.append(f"Trend{s:+.0f}(H4정렬)"); _ra_trend = s
            else:
                s = float(getattr(cfg, "guard_score_trend_low_conf", -5.0))
                total += s; bd.append(f"Trend{s:+.0f}(H4역행)"); _ra_trend = s
        # SIDEWAYS = 0
    except Exception as exc:
        logger.debug("[SPOT_CHAIN] Trend: %s", exc)

    # ── ⑭ 5M RSI 극단 변곡 (반전 자리) — portable (선물 2548-2564) ──
    try:
        from app.strategy.indicators import rsi_with_prev
        raw5 = _kl(client, market, "5", 20)
        if raw5 and len(raw5) >= 15:
            closes5 = [float(r[4]) for r in raw5 if len(r) >= 5]
            rsi_now5, rsi_prev5 = rsi_with_prev(closes5, length=14)
            if rsi_now5 is not None and rsi_prev5 is not None:
                if d == "LONG" and rsi_prev5 < 30 and rsi_now5 > rsi_prev5:
                    s = float(getattr(cfg, "guard_score_rsi_extreme", 10.0))
                    total += s; bd.append(f"RSI5M{s:+.0f}({rsi_prev5:.0f}<30↑)")
                elif d == "SHORT" and rsi_prev5 > 70 and rsi_now5 < rsi_prev5:
                    s = float(getattr(cfg, "guard_score_rsi_extreme", 10.0))
                    total += s; bd.append(f"RSI5M{s:+.0f}({rsi_prev5:.0f}>70↓)")
    except Exception as exc:
        logger.debug("[SPOT_CHAIN] RSI extreme: %s", exc)

    # ── ⑮ Flow Reversal Signal — SKIP(0점) ──
    #   _signal_history/_confidence_history 시계열 + scanner entry dict(adx_past) 의존. 현물 인프라 없음.

    # ── ⑯ Alt-BTC Alignment — SKIP(0점) ──
    #   _day_direction/confidence_history BTCUSDT 매크로 의존. ⑩(btc_dir)이 BTC 정렬을 이미 커버. _ra_altbtc=0.

    # ── ⑰⑱⑲⑳㉕ Peer 5종 (Win/SL/Struggle/Conflict/Crowding) — SKIP(0점) ──
    #   app.core.peer_brief 함대 인프라(다중서버 포지션 공유) 전적 의존. 현물 매니저엔 없음. 전부 0.

    # ── ㉑ Overextension (끝물 추격) — 선물 _check_overextension(3680-3725) 충실. 24h ticker→일봉 재구성 ──
    #   ★ range_pos_pct 는 0~100 → 선물 pos(0~1)·top(0.85) 스케일에 맞춰 /100. ADX 강돌파 면제 포함.
    try:
        if getattr(cfg, "overextension_enabled", False):
            m24 = _fetch_24h_metrics(client, market)
            if m24:
                min_move = float(getattr(cfg, "overextension_min_move_pct", 8.0))
                top = float(getattr(cfg, "overextension_range_pos_pct", 0.85))   # 0~1 (선물 동일)
                pen = float(getattr(cfg, "overextension_penalty", 10.0))
                pos = m24["range_pos_pct"] / 100.0                                # 0~100 → 0~1
                ext = (d == "LONG" and abs(m24["move_pct"]) >= min_move and pos >= top) \
                    or (d == "SHORT" and abs(m24["move_pct"]) >= min_move and pos <= (1.0 - top))
                if ext:
                    # ★ 강한 돌파 면제 — primary ADX ≥ adx_exempt 면 끝물 아님(벽타기) (선물 3715-3724)
                    adx_exempt = float(getattr(cfg, "overextension_adx_exempt", 30.0))
                    exempt = False
                    if adx_exempt > 0:
                        try:
                            from app.strategy import indicators as _ind
                            _r = _kl(client, market, str(getattr(cfg, "primary_tf", "240")), 60)
                            _a = _ind.adx([float(x[2]) for x in _r if len(x) >= 5],
                                          [float(x[3]) for x in _r if len(x) >= 5],
                                          [float(x[4]) for x in _r if len(x) >= 5])
                            _adxv = float(_a.get("adx", 0.0)) if _a else 0.0
                            if _adxv >= adx_exempt:
                                exempt = True
                                bd.append(f"Overext·0(돌파면제ADX{_adxv:.0f}≥{adx_exempt:.0f})")
                        except Exception:
                            pass
                    if not exempt:
                        total -= pen
                        bd.append(f"Overext-{pen:.0f}({'상단' if d == 'LONG' else '하단'}{pos * 100:.0f}%/{m24['move_pct']:+.0f}%)")
    except Exception as exc:
        logger.debug("[SPOT_CHAIN] Overext: %s", exc)

    # ── ㉔ Blow-off (24h 급등 추격) — 선물 _check_blowoff(3660-3675) 충실. 곱셈→선형보간 ──
    try:
        if getattr(cfg, "blowoff_filter_enabled", False):
            m24 = _fetch_24h_metrics(client, market)
            if m24:
                thr = float(getattr(cfg, "blowoff_move_pct", 30.0))
                chase_only = bool(getattr(cfg, "blowoff_chase_only", True))
                mv = m24["move_pct"]
                move = abs(mv)
                chasing = (d == "LONG" and mv > 0) or (d == "SHORT" and mv < 0)
                if move >= thr and (chasing or not chase_only):
                    base = float(getattr(cfg, "blowoff_penalty", 20.0))
                    ext = float(getattr(cfg, "blowoff_extreme_pct", 80.0))
                    maxp = float(getattr(cfg, "blowoff_max_penalty", 40.0))
                    if move >= ext:
                        eff = maxp
                    else:
                        span = max(1e-9, ext - thr)
                        eff = base + (move - thr) / span * (maxp - base)
                    eff = max(0.0, min(maxp, eff))
                    total -= eff; bd.append(f"Blowoff-{eff:.0f}({mv:+.0f}% 추격)")
    except Exception as exc:
        logger.debug("[SPOT_CHAIN] Blowoff: %s", exc)

    # ── ㉒ Inflection Setup (변곡 점수) — portable (선물 2640-2649) ──
    try:
        if getattr(cfg, "inflection_setup_enabled", False):
            _if_mod = _inflection_setup(client, market, d, cfg)
            if _if_mod != 0.0:
                total += _if_mod; bd.append(f"Inflect{_if_mod:+.0f}")
    except Exception as exc:
        logger.debug("[SPOT_CHAIN] Inflection: %s", exc)

    # ── ㉓ Retest Setup (돌파→눌림→지지) — portable (선물 2650-2658) ──
    try:
        if getattr(cfg, "retest_setup_enabled", False):
            _rt_mod = _retest_setup(client, market, d, cfg)
            if _rt_mod > 0.0:
                total += _rt_mod; bd.append(f"Retest+{_rt_mod:.0f}")
    except Exception as exc:
        logger.debug("[SPOT_CHAIN] Retest: %s", exc)

    # ── DirDedupe (방향 이중계산 제거) — portable 산술 (선물 2659-2670) ──
    #   AltBTC/BTC정렬 skip 시 _ra_altbtc/_ra_btcalign=0 → 자동 무해(Frame/Trend 만 차감).
    if getattr(cfg, "guard_dir_dedupe_enabled", False):
        try:
            _dd_sum = _ra_frame + _ra_trend + _ra_altbtc + _ra_btcalign
            if _dd_sum != 0.0:
                total -= _dd_sum
                bd.append(f"DirDedupe[F{_ra_frame:+.0f}/T{_ra_trend:+.0f}/AltB{_ra_altbtc:+.0f}/BTC{_ra_btcalign:+.0f}=-{_dd_sum:+.0f}]")
        except Exception as exc:
            logger.debug("[SPOT_CHAIN] DirDedupe: %s", exc)
    # ── RegimeAlignCap (추세정렬 합산 캡) — portable 산술, dedupe OFF 일 때만 (선물 2671-2681) ──
    elif getattr(cfg, "regime_align_cap_enabled", False):
        try:
            _ra_raw = _ra_frame + _ra_trend + _ra_altbtc
            _ra_cap = float(getattr(cfg, "regime_align_cap", 15.0))
            _ra_clamped = max(-_ra_cap, min(_ra_cap, _ra_raw))
            if _ra_clamped != _ra_raw:
                total += (_ra_clamped - _ra_raw)
                bd.append(f"RegimeCap[{_ra_raw:+.0f}→{_ra_clamped:+.0f}]")
        except Exception as exc:
            logger.debug("[SPOT_CHAIN] RegimeCap: %s", exc)

    # ── TotalCap (가산점 총합 캡) — portable 산술 (선물 2682-2688) ──
    if getattr(cfg, "guard_score_total_cap_enabled", False):
        _cap = abs(float(getattr(cfg, "guard_score_total_cap", 80.0)))
        if _cap > 0 and abs(total) > _cap:
            clamped = max(-_cap, min(_cap, total))
            bd.append(f"TotalCap[{total:+.0f}→{clamped:+.0f}]")
            total = clamped

    return round(total, 1), bd


# ============================================================
# ⑦⑧⑨ portable 헬퍼 — 선물 _check_microtiming_5m / _check_raw_body_guard /
#   _check_momentum_derivative_guard 의 순수 부분만 이식 (BLOCK 판정 → guard_score 점수용 bool).
# ============================================================
def _microtiming_5m_ok(client: Any, market: str, direction: str, cfg: Any) -> bool:
    """⑦ 5M RSI/MACD/BB 변곡 3종 점수 ≥ min → True. 원본 focus_manager.py:7911-8000+."""
    try:
        from app.strategy.indicators import rsi_with_prev, macd_hist_pair, bollinger_bands
        raw = _kl(client, market, "5", 30)
        if not raw or len(raw) < 27:
            return True   # fetch 부족 → 무효(안전 측, ok)
        closes = [float(r[4]) for r in raw if len(r) >= 5]
        rsi_now, rsi_prev = rsi_with_prev(closes, length=14)
        hist_now, hist_prev = macd_hist_pair(closes)
        bb_now = bollinger_bands(closes, 20, 2.0)
        bb_prev = bollinger_bands(closes[:-1], 20, 2.0) if len(closes) >= 21 else None

        def _bb_pct(bb, price):
            if not bb or bb.get("upper", 0) <= bb.get("lower", 0):
                return None
            return (price - bb["lower"]) / (bb["upper"] - bb["lower"]) * 100.0
        bb_now_pct = _bb_pct(bb_now, closes[-1])
        bb_prev_pct = _bb_pct(bb_prev, closes[-2]) if bb_prev and len(closes) >= 2 else None
        rsi_thr = float(getattr(cfg, "microtiming_5m_rsi_long_threshold", 35.0))
        bb_low = float(getattr(cfg, "microtiming_5m_bb_low_pct", 20.0))
        bb_rec = float(getattr(cfg, "microtiming_5m_bb_recover_pct", 30.0))
        rsi_s = macd_s = bb_s = 0
        if rsi_prev is not None and rsi_now is not None and rsi_prev <= rsi_thr and rsi_now > rsi_prev:
            rsi_s = 1
        if hist_prev is not None and hist_now is not None and hist_prev < 0 and (hist_now > 0 or hist_now > hist_prev):
            macd_s = 1
        if bb_prev_pct is not None and bb_now_pct is not None and bb_prev_pct < bb_low and bb_now_pct >= bb_rec:
            bb_s = 1
        total = rsi_s + macd_s + bb_s
        min_score = int(getattr(cfg, "microtiming_5m_min_score", 2))
        return total >= min_score
    except Exception as exc:
        logger.debug("[SPOT_CHAIN] _microtiming_5m_ok: %s", exc)
        return True


def _raw_body_against(client: Any, market: str, direction: str, cfg: Any) -> bool:
    """⑧ 직전 N완성봉 시가→종가 net 이 진입 반대 = blocked → True. 원본 focus_manager.py:11210-11251."""
    try:
        lookback = max(1, int(getattr(cfg, "raw_body_guard_lookback", getattr(cfg, "raw_body_lookback", 3))))
        min_net = float(getattr(cfg, "raw_body_guard_min_net_pct", getattr(cfg, "raw_body_min_net_pct", 0.05)))
        raw = _kl(client, market, "5", lookback + 1)
        if not raw or len(raw) < lookback + 1:
            return False
        recent = raw[-(lookback + 1):-1]
        if not recent:
            return False
        ref = float(recent[-1][4]) or 0.0
        if ref <= 0:
            return False
        net_pct = sum(float(b[4]) - float(b[1]) for b in recent) / ref * 100.0
        du = (direction or "").upper()
        if du == "LONG" and net_pct < -abs(min_net):
            return True
        if du == "SHORT" and net_pct > abs(min_net):
            return True
        return False
    except Exception as exc:
        logger.debug("[SPOT_CHAIN] _raw_body_against: %s", exc)
        return False


def _momentum_deriv_against(client: Any, market: str, direction: str, cfg: Any) -> bool:
    """⑨ 5M RSI/MACD 변화율이 진입 반대 가속 = blocked → True. 원본 focus_manager.py:11253+."""
    try:
        from app.strategy.indicators import rsi_series, macd_hist_series
        du = (direction or "").upper()
        if du not in ("LONG", "SHORT"):
            return False
        lookback = max(2, int(getattr(cfg, "momentum_deriv_guard_lookback", getattr(cfg, "momentum_deriv_lookback", 5))))
        rsi_thr = float(getattr(cfg, "momentum_deriv_guard_rsi_min_slope", getattr(cfg, "momentum_deriv_rsi_slope", 2.0)))
        macd_thr = float(getattr(cfg, "momentum_deriv_guard_macd_min_slope", getattr(cfg, "momentum_deriv_macd_slope", 0.0)))
        require_both = bool(getattr(cfg, "momentum_deriv_guard_require_both", getattr(cfg, "momentum_deriv_require_both", True)))
        tf = str(getattr(cfg, "momentum_deriv_guard_tf", "5"))
        need = 14 + lookback * 2 + 5
        raw = _kl(client, market, tf, need)
        if not raw or len(raw) < need:
            return False
        closes = [float(r[4]) for r in raw if len(r) >= 5]
        rsi_vals = rsi_series(closes, 14)
        macd_hist = macd_hist_series(closes, 12, 26, 9)
        if not rsi_vals or len(rsi_vals) < lookback * 2:
            return False
        if not macd_hist or len(macd_hist) < lookback * 2:
            return False
        rsi_delta = sum(rsi_vals[-lookback:]) / lookback - sum(rsi_vals[-2 * lookback:-lookback]) / lookback
        macd_delta = sum(macd_hist[-lookback:]) / lookback - sum(macd_hist[-2 * lookback:-lookback]) / lookback
        if du == "LONG":
            rsi_against = rsi_delta < -abs(rsi_thr)
            macd_against = macd_delta < -abs(macd_thr)
        else:
            rsi_against = rsi_delta > abs(rsi_thr)
            macd_against = macd_delta > abs(macd_thr)
        return (rsi_against and macd_against) if require_both else (rsi_against or macd_against)
    except Exception as exc:
        logger.debug("[SPOT_CHAIN] _momentum_deriv_against: %s", exc)
        return False


# ============================================================
# ㉒㉓ portable 헬퍼 — 선물 _compute_inflection_setup(3730-3792) /
#   _compute_retest_setup(3794-3884) 의 순수 kline+내장수식 이식.
# ============================================================
def _inflection_setup(client: Any, market: str, direction: str, cfg: Any) -> float:
    """㉒ 24h(5m 288봉) 위치 × 15분 모멘텀 → 천장stall 감점/바닥변곡 가점. ±cap.
    선물 _compute_inflection_setup(focus_manager.py:3744-3789) verbatim 이식
    (slope15m %·EMA12/26-MACD hist Δ lean·at_high/at_low 방향게이팅). _get_mtf_kline→_kl 만 적응."""
    try:
        import math
        raw = _kl(client, market, "5", 288)
        if not raw or len(raw) < 35:
            return 0.0
        cl = [float(r[4]) for r in raw if len(r) >= 5]
        his = [float(r[2]) for r in raw if len(r) >= 5]
        los = [float(r[3]) for r in raw if len(r) >= 5]
        if len(cl) < 35:
            return 0.0
        hi = max(his); lo = min(los); last = cl[-1]
        if hi <= lo or last <= 0:
            return 0.0
        pos = (last - lo) / (hi - lo)
        slope15m = (cl[-1] - cl[-4]) / cl[-4] * 100.0 if cl[-4] != 0 else 0.0

        def _ema(vals, n):
            k = 2.0 / (n + 1); e = vals[0]; out = [e]
            for v in vals[1:]:
                e = v * k + e * (1 - k); out.append(e)
            return out

        e12 = _ema(cl, 12); e26 = _ema(cl, 26)
        macd_line = [a - b for a, b in zip(e12, e26)]
        sig = _ema(macd_line, 9)
        histl = [a - b for a, b in zip(macd_line, sig)]
        hist_d = histl[-1] - histl[-2] if len(histl) >= 2 else 0.0
        sscale = float(getattr(cfg, "inflection_setup_slope_scale", 0.40)) or 0.40
        lean = 0.25 * (1 if hist_d > 0 else (-1 if hist_d < 0 else 0))
        up = max(-1.0, min(1.0, 0.75 * math.tanh(slope15m / sscale) + lean))
        at_high = max(0.0, pos - 0.5) * 2.0
        at_low = max(0.0, 0.5 - pos) * 2.0
        base = float(getattr(cfg, "inflection_setup_base", 0.45))
        W = float(getattr(cfg, "inflection_setup_weight", 20.0))
        cap = float(getattr(cfg, "inflection_setup_cap", 20.0))
        if (direction or "").upper() == "LONG":
            val = W * (at_high * (up - base) + at_low * (base + up))
        else:
            val = W * (at_high * (base - up) + at_low * (-base - up))
        return max(-cap, min(cap, round(val, 1)))
    except Exception as exc:
        logger.debug("[SPOT_CHAIN] _inflection_setup: %s", exc)
        return 0.0


def _retest_setup(client: Any, market: str, direction: str, cfg: Any) -> float:
    """㉓ 5m 60봉 피벗 돌파 후 되돌림+turning = 좋은 진입자리 가점.
    선물 _compute_retest_setup(focus_manager.py:3812-3884) verbatim 이식
    (_pivots ±W swing·BRK 돌파·post극값·retr=(post-price)/(post-level)·q소프트·FAIL이탈·RETR_HI+0.3 slack).
    _get_mtf_kline→_kl 만 적응. 거부=0.0(선물의 (0.0,"") 와 동일 의미)."""
    try:
        raw = _kl(client, market, "5", 60)
        if not raw or len(raw) < 20:
            return 0.0
        hi = [float(r[2]) for r in raw if len(r) >= 5]
        lo = [float(r[3]) for r in raw if len(r) >= 5]
        cl = [float(r[4]) for r in raw if len(r) >= 5]
        op = [float(r[1]) for r in raw if len(r) >= 5]
        if len(cl) < 20:
            return 0.0
        n = len(cl); price = cl[-1]
        W = int(getattr(cfg, "retest_pivot_width", 2))
        BRK = 0.001
        RETR_LO = float(getattr(cfg, "retest_retr_lo", 0.30))
        RETR_HI = float(getattr(cfg, "retest_retr_hi", 0.90))
        FAIL = float(getattr(cfg, "retest_fail_pct", 0.005))
        W_RET = float(getattr(cfg, "retest_setup_weight", 12.0))
        TURN = float(getattr(cfg, "retest_setup_turn_bonus", 4.0))
        up = cl[-1] > op[-1] or (len(cl) >= 2 and cl[-1] > cl[-2])
        dn = cl[-1] < op[-1] or (len(cl) >= 2 and cl[-1] < cl[-2])

        def _pivots(vals, ishigh):
            out = []
            for i in range(W, len(vals) - W):
                seg = vals[i - W:i + W + 1]
                if ishigh and vals[i] == max(seg):
                    out.append((i, vals[i]))
                if (not ishigh) and vals[i] == min(seg):
                    out.append((i, vals[i]))
            return out

        if (direction or "").upper() == "LONG":
            broken = None
            for idx, res in _pivots(hi, True):
                if any(hi[j] > res * (1 + BRK) for j in range(idx + 1, n)):
                    broken = (idx, res)
            if not broken:
                return 0.0
            ridx, res = broken
            post_hi = max(hi[ridx:])
            if post_hi <= res:
                return 0.0
            retr = (post_hi - price) / (post_hi - res)
            if price < res * (1 - FAIL):
                return 0.0
            if retr < RETR_LO or retr > RETR_HI + 0.3:
                return 0.0
            q = max(0.0, 1 - abs(retr - 0.6) / 0.5)
            sc = W_RET * q + (TURN if up else 0.0)
        else:
            broken = None
            for idx, sup in _pivots(lo, False):
                if any(lo[j] < sup * (1 - BRK) for j in range(idx + 1, n)):
                    broken = (idx, sup)
            if not broken:
                return 0.0
            sidx, sup = broken
            post_lo = min(lo[sidx:])
            if post_lo >= sup:
                return 0.0
            retr = (price - post_lo) / (sup - post_lo)
            if price > sup * (1 + FAIL):
                return 0.0
            if retr < RETR_LO or retr > RETR_HI + 0.3:
                return 0.0
            q = max(0.0, 1 - abs(retr - 0.6) / 0.5)
            sc = W_RET * q + (TURN if dn else 0.0)
        if sc <= 0.0:
            return 0.0
        return round(sc, 1)
    except Exception as exc:
        logger.debug("[SPOT_CHAIN] _retest_setup: %s", exc)
        return 0.0


# ============================================================
# adx_entry_gate — 선물 ADX 상태머신 진입 게이트 (focus_manager.py:13266+/7417+) 이식
#   "시장에 추세가 있나" 거시 게이트. adx_filter_enabled 면 primary_tf ADX < min_adx_entry → 거부.
#   ★ fail-open: 데이터 없으면 통과(블록 안 함).
# ============================================================
def adx_entry_gate(
    client: Any,
    market: str,
    cfg: Any,
    primary_tf: Optional[str] = None,
) -> Tuple[bool, str]:
    """SIDEWAYS/저ADX 거부 게이트. 원본 _fetch_primary_adx(7417-7453) + DORMANT 전이(13283+).

    Returns (allowed: bool, reason: str).
      ─ adx_filter_enabled=False → (True, "adx_filter_off")  [★스펙: 라이브에선 True 유지 권장]
      ─ adx < min_adx_entry → (False, 사유)  [저ADX junk 차단]
      ─ 데이터 부족/에러 → (True, fail-open)  [블록 안 함]
    """
    if not getattr(cfg, "adx_filter_enabled", True):
        return True, "adx_filter_off"
    # ★ [2026-06-19 부모] 게이트 전용 TF — primary_tf(H4) 대신 adx_entry_tf(H1) 우선.
    #   caller 가 primary_tf 명시하면 그걸, 없으면 cfg.adx_entry_tf(H1), 그것도 없으면 primary_tf(H4).
    tf = str(primary_tf or getattr(cfg, "adx_entry_tf", None) or getattr(cfg, "primary_tf", "240"))
    try:
        from app.strategy import indicators
        raw = _kl(client, market, tf, 60)
        highs = [float(r[2]) for r in raw if len(r) >= 5]
        lows = [float(r[3]) for r in raw if len(r) >= 5]
        closes = [float(r[4]) for r in raw if len(r) >= 5]
        if len(closes) < 2 * 14 + 1:        # ADX(14) 최소 29봉
            return True, "adx_no_data"       # fail-open
        a = indicators.adx(highs, lows, closes)
        if not a:
            return True, "adx_calc_none"     # fail-open
        adx = float(a.get("adx", 0.0) or 0.0)
        min_entry = float(getattr(cfg, "min_adx_entry", 17))
        if adx < min_entry:
            # ★ 돌파 면제 — 저ADX(추세 막 시작)여도 직전 *닫힌* 봉이 최근 N봉 고가를 돌파했으면 통과.
            #   박스 회복/돌파 진입을 ADX가 따라오기 전에 허용(부모님 'TRUST 회복 진입' 케이스).
            #   끝물 추격은 상단 overext/blowoff(일봉) 게이트가 별도로 막으므로 안전.
            if getattr(cfg, "adx_entry_breakout_exempt", True):
                try:
                    _lb = int(getattr(cfg, "adx_entry_breakout_lookback", 12) or 12)
                    _ch, _hh = closes[:-1], highs[:-1]   # forming 봉 제외(닫힌 봉만)
                    if len(_ch) > _lb + 1:
                        _last = _ch[-1]
                        _prior_hi = max(_hh[-(_lb + 1):-1])   # 직전 봉 *이전* N봉 최고가
                        if _last > _prior_hi:
                            return True, f"breakout 면제(close {_last:.4g}>최근{_lb}봉고가 {_prior_hi:.4g}, adx {adx:.1f})"
                except Exception:
                    pass
            return False, f"adx {adx:.1f}<{min_entry:.0f} (SIDEWAYS/저ADX 추세없음)"
        return True, f"adx_ok({adx:.1f}≥{min_entry:.0f})"
    except Exception as exc:
        logger.debug("[SPOT_CHAIN] adx_entry_gate %s: %s", market, exc)
        return True, "adx_error"             # fail-open
