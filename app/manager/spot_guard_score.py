# ============================================================
# Upbit FOCUS guard_score — 진입 확신 점수 (G1: ADX + 추세 confidence)
# ------------------------------------------------------------
# Bybit guard_score(focus_manager._compute_guard_score_modifiers) 의 Upbit-native 이식.
# ★ "점수=차트와 맞아야" (feedback_score_must_match_chart) — 좋은 자리일수록 +.
# ★ 65-80 sweet / 80+ 후행 비대칭(저널분석) → blind ADX floor 금지, 균형 점수 + total_cap(G4).
#
# 단계 (DESIGN_spot_guard_score_port_20260617.md):
#   G1(여기) — ADX strong/weak + trend conf high/low. 표시·관측용(threshold=0 이면 게이트 X).
#   G2~ — PA완성/Frame/Anchor/BTC정렬/Vol/RSI. G4 — total_cap + threshold 게이트.
#
# 순수 함수 — I/O·상태 없음. (총점, 내역 리스트) 반환.
# ============================================================
from __future__ import annotations

from typing import List, Tuple

# ── G1 컴포넌트 상수 (Bybit guard_score 기본값 — focus_manager.py:1327~) ──
_ADX_STRONG_THR = 30.0    # ADX ≥ 30 = 강추세
_ADX_WEAK_THR = 20.0      # ADX < 20 = 약추세(횡보)
_ADX_STRONG_PTS = 10.0
_ADX_WEAK_PTS = -5.0
_TREND_HIGH_CONF = 0.75   # 추세 confidence ≥ 0.75 = 뚜렷
_TREND_LOW_CONF = 0.50    # < 0.50 = 모호
_TREND_HIGH_PTS = 10.0
_TREND_LOW_PTS = -5.0
# ── G2 컴포넌트 (PA 완성 ⭐ + Frame 정렬, Bybit guard_score 가중치 동일) ──
_PA_OK_PTS = 30.0         # 정렬된 PA 패턴 완성 (⭐ 부모님 핵심 — 좋은 자리)
_PA_NONE_PTS = -10.0      # PA 없음 또는 역방향
_PA_MIN_CONF = 0.5
_FRAME_ALIGNED_PTS = 15.0  # 추세 방향 일치 (UPTREND + LONG)
_FRAME_NEUTRAL_PTS = 5.0   # SIDEWAYS (중립 자리)
_FRAME_OPPOSITE_PTS = -20.0  # DOWNTREND (역행 — 보통 상류 차단, 방어적)
# ── G3 컴포넌트 (BTC 정렬 + Anchor 눌림목 근접) ──
_BTC_ALIGNED_PTS = 15.0    # BTC UP + LONG = 순풍
_BTC_OPPOSITE_PTS = -15.0  # BTC DOWN = 역풍
_ANCHOR_PTS = 20.0         # 눌림목 — 가장 가까운 SUPPORT 에 근접(사이클 시작점, 좋은 진입)
_ANCHOR_PROX_ATR = 1.0     # SUPPORT 까지 거리 ≤ 1×ATR = 근접
# ── G5 컴포넌트 (Vol 동반 + RSI 과매도 반등 — 미세 가점) ──
_VOL_BIG_PTS = 10.0        # 거래량 평균 대비 큼 + 상승봉 = 매수세 동반
_VOL_MULT = 2.0
_RSI_PTS = 10.0            # 5/primary RSI 과매도(<30) + 상승 변곡 = 반등 시작(LONG)
_RSI_OVERSOLD = 30.0

# ── 672 canonical guard_score 가중치 (선물 FocusConfig 와 동일 이름·기본값) ──
#   Ch2 (2026-06-18): 옛 하드코딩 상수를 SpotGazuaConfig 672 필드로 config-drive.
#   기본값 = 위 상수와 동일(=선물 672 기본) → cfg 미주입/미변경 시 동작 0변경. UI 조정 시 즉시 반영.
_DEFAULT_GS_WEIGHTS = {
    "guard_score_adx_strong": _ADX_STRONG_PTS,
    "guard_score_adx_weak": _ADX_WEAK_PTS,
    "guard_score_trend_high_conf": _TREND_HIGH_PTS,
    "guard_score_trend_low_conf": _TREND_LOW_PTS,
    "guard_score_pa_completion_ok": _PA_OK_PTS,
    "guard_score_pa_completion_none": _PA_NONE_PTS,
    "guard_score_frame_aligned": _FRAME_ALIGNED_PTS,
    "guard_score_frame_neutral": _FRAME_NEUTRAL_PTS,
    "guard_score_frame_opposite": _FRAME_OPPOSITE_PTS,
    "guard_score_btc_aligned": _BTC_ALIGNED_PTS,
    "guard_score_btc_opposite": _BTC_OPPOSITE_PTS,
    "guard_score_anchor_close": _ANCHOR_PTS,
    "guard_score_vol_big_align": _VOL_BIG_PTS,
    "guard_score_rsi_extreme": _RSI_PTS,
}


def gs_weights_from_config(cfg) -> dict:
    """SpotGazuaConfig(672) 에서 guard_score_* 가중치 추출. cfg None/필드없음 → 기본 상수(=선물 672 기본).
    값이 선물 672 기본과 동일하면 동작 0변경, 부모님이 UI 로 조정 시 즉시 반영."""
    if cfg is None:
        return dict(_DEFAULT_GS_WEIGHTS)
    return {k: float(getattr(cfg, k, v)) for k, v in _DEFAULT_GS_WEIGHTS.items()}


def compute_guard_score(
    highs: List[float],
    lows: List[float],
    closes: List[float],
    trend_conf: float,
    *,
    trend: str = "",
    pa_direction: str = "",
    pa_confidence: float = 0.0,
    btc_direction: str = "",
    price: float = 0.0,
    zones: List[dict] = None,
    atr: float = 0.0,
    volumes: List[float] = None,
    total_cap: float = 0.0,
    weights: dict = None,
) -> Tuple[float, List[str]]:
    """guard_score = G1(ADX+추세conf) + G2(PA완성 ⭐ + Frame) + G3(BTC정렬 + Anchor) + G5(Vol + RSI). (총점, 내역).

    ★ floor 아님 — 가산 점수(좋은 자리 확신). total_cap>0 이면 ±cap 클램프(80+ 억제, G4 준비).
    데이터 부족/계산 실패 시 해당 컴포넌트만 건너뜀(0점, fail-open).

    ★ SIDEWAYS-ADX 보정 (2026-06-17 부모): 구조가 SIDEWAYS 면 ADX 강추세 가점 *면제*.
      횡보 합의 구간에서 ADX 가 스윙 잔상으로 ≥30 찍혀 점수가 차트(횡보)와 어긋나는 것 교정.
      (방향 없는 ADX 는 진짜 추세 아님. 약추세 감점은 유지 — 횡보는 깎여야.)

    Args (G2): pa_direction/pa_confidence = GreenPen 최우선 PA 신호(현물 long_only → LONG 정렬만 +).
    """
    # ── 672 config-drive: weights 주입 시 해당 가중치로 덮음(미주입=기본 상수). 로컬 섀도잉 → 본문 무수정 ──
    _W = _DEFAULT_GS_WEIGHTS if not weights else {**_DEFAULT_GS_WEIGHTS, **weights}
    _ADX_STRONG_PTS = _W["guard_score_adx_strong"]
    _ADX_WEAK_PTS = _W["guard_score_adx_weak"]
    _TREND_HIGH_PTS = _W["guard_score_trend_high_conf"]
    _TREND_LOW_PTS = _W["guard_score_trend_low_conf"]
    _PA_OK_PTS = _W["guard_score_pa_completion_ok"]
    _PA_NONE_PTS = _W["guard_score_pa_completion_none"]
    _FRAME_ALIGNED_PTS = _W["guard_score_frame_aligned"]
    _FRAME_NEUTRAL_PTS = _W["guard_score_frame_neutral"]
    _FRAME_OPPOSITE_PTS = _W["guard_score_frame_opposite"]
    _BTC_ALIGNED_PTS = _W["guard_score_btc_aligned"]
    _BTC_OPPOSITE_PTS = _W["guard_score_btc_opposite"]
    _ANCHOR_PTS = _W["guard_score_anchor_close"]
    _VOL_BIG_PTS = _W["guard_score_vol_big_align"]
    _RSI_PTS = _W["guard_score_rsi_extreme"]
    total = 0.0
    bd: List[str] = []
    _is_sideways = str(trend or "").upper() == "SIDEWAYS"

    # ── ADX (강추세 +/약추세 -) ──
    try:
        from app.strategy import indicators
        a = indicators.adx(highs, lows, closes)
        if a:
            adx = float(a.get("adx", 0) or 0)
            if adx >= _ADX_STRONG_THR:
                if _is_sideways:
                    bd.append(f"ADX·0({adx:.0f}, 횡보=가점면제)")   # 구조 횡보 → 강추세 가점 면제
                else:
                    total += _ADX_STRONG_PTS
                    bd.append(f"ADX+{_ADX_STRONG_PTS:.0f}({adx:.0f})")
            elif adx < _ADX_WEAK_THR:
                total += _ADX_WEAK_PTS
                bd.append(f"ADX{_ADX_WEAK_PTS:.0f}({adx:.0f})")
            else:
                bd.append(f"ADX·0({adx:.0f})")
    except Exception:
        pass

    # ── 추세 confidence (뚜렷 +/모호 -) ──
    tc = float(trend_conf or 0)
    if tc >= _TREND_HIGH_CONF:
        total += _TREND_HIGH_PTS
        bd.append(f"Trend+{_TREND_HIGH_PTS:.0f}({tc:.2f})")
    elif tc < _TREND_LOW_CONF:
        total += _TREND_LOW_PTS
        bd.append(f"Trend{_TREND_LOW_PTS:.0f}({tc:.2f})")
    else:
        bd.append(f"Trend·0({tc:.2f})")

    # ── PA 완성 (G2 ⭐) — 정렬된(LONG) PA 패턴 = 좋은 자리 ──
    pad = str(pa_direction or "").upper()
    if pad == "LONG" and float(pa_confidence or 0) >= _PA_MIN_CONF:
        total += _PA_OK_PTS
        bd.append(f"PA+{_PA_OK_PTS:.0f}({float(pa_confidence):.2f})")
    elif pad in ("", "SHORT"):   # PA 없음 또는 역방향(현물 long_only)
        total += _PA_NONE_PTS
        bd.append(f"PA{_PA_NONE_PTS:.0f}")
    # (LONG 인데 conf 낮음 → 0, 중립)

    # ── Frame 정렬 (G2) — 추세 방향 일치 ──
    tu = str(trend or "").upper()
    if tu == "UPTREND":
        total += _FRAME_ALIGNED_PTS
        bd.append(f"Frame+{_FRAME_ALIGNED_PTS:.0f}(정렬)")
    elif tu == "SIDEWAYS":
        total += _FRAME_NEUTRAL_PTS
        bd.append(f"Frame+{_FRAME_NEUTRAL_PTS:.0f}(중립)")
    elif tu == "DOWNTREND":
        total += _FRAME_OPPOSITE_PTS
        bd.append(f"Frame{_FRAME_OPPOSITE_PTS:.0f}(역행)")

    # ── BTC 정렬 (G3) — BTC 순풍/역풍 (현물 long_only) ──
    bdir = str(btc_direction or "").upper()
    if bdir == "UP":
        total += _BTC_ALIGNED_PTS
        bd.append(f"BTC+{_BTC_ALIGNED_PTS:.0f}(순풍)")
    elif bdir == "DOWN":
        total += _BTC_OPPOSITE_PTS
        bd.append(f"BTC{_BTC_OPPOSITE_PTS:.0f}(역풍)")

    # ── Anchor 눌림목 (G3) — 가장 가까운 SUPPORT 에 근접 = 사이클 시작점(좋은 진입) ──
    if zones and price > 0 and atr > 0:
        try:
            supports_below = [
                float(z.get("price_high", 0) or 0)
                for z in zones
                if str(z.get("type", "")).upper() == "SUPPORT"
                and float(z.get("price_high", 0) or 0) <= price
            ]
            if supports_below:
                nearest = max(supports_below)         # 현재가 바로 아래 가장 가까운 지지
                prox_atr = (price - nearest) / atr
                if prox_atr <= _ANCHOR_PROX_ATR:
                    total += _ANCHOR_PTS
                    bd.append(f"Anchor+{_ANCHOR_PTS:.0f}(지지 {prox_atr:.1f}ATR)")
        except Exception:
            pass

    # ── Vol 동반 (G5) — 거래량 평균 2x+ & 상승봉 = 매수세 동반 ──
    if volumes and len(volumes) >= 5:
        try:
            cur_v = float(volumes[-1] or 0)
            prev_v = [float(v or 0) for v in volumes[:-1]]
            avg_v = sum(prev_v) / max(len(prev_v), 1)
            up_bar = len(closes) >= 2 and closes[-1] >= closes[-2]
            if avg_v > 0 and cur_v >= avg_v * _VOL_MULT and up_bar:
                total += _VOL_BIG_PTS
                bd.append(f"Vol+{_VOL_BIG_PTS:.0f}({cur_v / avg_v:.1f}x↑)")
        except Exception:
            pass

    # ── RSI 과매도 반등 (G5) — RSI<30 + 상승 변곡 = 반등 시작(LONG 변곡) ──
    try:
        from app.strategy import indicators
        _r = indicators.rsi_with_prev(closes)
        if _r:
            cur_rsi, prev_rsi = _r
            if (cur_rsi is not None and cur_rsi < _RSI_OVERSOLD
                    and (prev_rsi is None or cur_rsi > prev_rsi)):
                total += _RSI_PTS
                bd.append(f"RSI+{_RSI_PTS:.0f}({cur_rsi:.0f}<30↑)")
    except Exception:
        pass

    if total_cap > 0:
        total = max(-total_cap, min(total_cap, total))
    return round(total, 1), bd
