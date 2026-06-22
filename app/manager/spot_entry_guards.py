# ============================================================
# Spot Entry Guards — 선물 FOCUS 진입 타이밍 게이트 *복사*-이식 (Phase 1)
# ------------------------------------------------------------
# 원본: focus_manager.py 의 검증된 진입 게이트 (라이브 선물 무손상, 여기로 복사).
#   - check_gap            ← gap_check       (focus_manager.py:16694-16737)
#   - check_micro_1m       ← _check_micro_1m (focus_manager.py:8013-8094)
#   - check_momentum_reversal ← _compute_momentum_reversal_penalty (8096-8162)
# 보존 규칙: long-only(SHORT 가지 보존되나 현물은 LONG만 전달) · ADX 면제 미적용(천장 누수 방지).
# 순수 함수 — self 상태 0, (client, market, direction, cfg)만. 캔들은 client.get_kline(TTL 캐시).
# 모두 default OFF (cfg.*_enabled=False) → paper 관측 후 ON.
# ============================================================
from __future__ import annotations

import logging
from typing import Any, Tuple

logger = logging.getLogger(__name__)


def check_gap(client: Any, market: str, direction: str, price: float, atr: float, cfg: Any) -> Tuple[bool, str]:
    """진입 전 갭 체크 — 선택 TF×N봉 고가(LONG)까지 거리 < 필요갭 → 차단(천장 바로 밑 진입 금지).
    필요갭 = max(min_pct, ATR%×atr_mult) 상한 atr_cap_pct (등락폭 큰 코인은 더 아래에서만).
    원본 focus_manager.py:16694-16737. fail-open(데이터 없으면 통과)."""
    if not getattr(cfg, "gap_check_enabled", False):
        return True, ""
    min_pct = float(getattr(cfg, "gap_check_min_pct", 0.3) or 0.0)
    if min_pct <= 0 or price <= 0:
        return True, ""
    try:
        tf = str(getattr(cfg, "gap_check_tf", "15"))
        bars = int(getattr(cfg, "gap_check_lookback_bars", 12))
        raw = client.get_kline(market, interval=tf, limit=bars + 2)
        if not raw or len(raw) < bars:
            return True, "gap_no_data"
        recent = raw[-bars:]
        if (direction or "").upper() == "LONG":
            wall = max(float(r[2]) for r in recent if len(r) >= 5)   # 위 N봉 고가
            gap = (wall - price) / price * 100.0
        else:
            wall = min(float(r[3]) for r in recent if len(r) >= 5)
            gap = (price - wall) / price * 100.0
        eff = min_pct
        if getattr(cfg, "gap_check_atr_adaptive", True) and atr > 0:
            atrp = atr / price * 100.0
            need = atrp * float(getattr(cfg, "gap_check_atr_mult", 0.7))
            cap = float(getattr(cfg, "gap_check_atr_cap_pct", 1.5))
            eff = max(min_pct, min(need, cap))
        # ★ [2026-06-20] 돌파 면제 — 직전(마지막) 봉이 그 전 N-1봉 고가를 돌파(신고가)했으면 = 진짜 돌파지 천장추격 아님 → 통과.
        #   돌파하는 코인은 늘 자기 고가 코앞이라 앵커만으론 못 푸는 케이스(예: KERNEL BOS_BULLISH·room10%인데 gap 0.25%<1.03% 차단).
        #   펌프탑/끝물은 headroom·overextension·micro_1m 게이트가 별도로 막으므로 gap만 면제해도 안전.
        if (direction or "").upper() == "LONG" and getattr(cfg, "gap_check_breakout_exempt", True):
            prior = recent[:-1]
            if len(prior) >= 2:
                prior_wall = max(float(r[2]) for r in prior if len(r) >= 5)
                last_high = float(recent[-1][2]) if len(recent[-1]) >= 5 else 0.0
                if prior_wall > 0 and last_high > prior_wall:
                    return True, f"gap_breakout_exempt(신고가 {last_high:.4f}>직전 {prior_wall:.4f} 돌파)"
        if gap < eff:
            return False, f"gap {gap:.2f}%<{eff:.2f}% ({tf}M×{bars}봉 wall={wall:.4f} 천장추격)"
        return True, "gap_ok"
    except Exception as exc:
        logger.debug("[SPOT_GUARD] gap_check fail-open: %s", exc)
        return True, "gap_error"


def check_micro_1m(client: Any, market: str, direction: str, cfg: Any) -> Tuple[bool, str]:
    """1M 마이크로 타이밍 — "지금 이 순간" 검증. 역봉/거래량소진/RSI과열이면 이번 tick 보류.
    원본 focus_manager.py:8013-8094. ★ADX 면제 미적용(현물: 잔파동도 따짐). fail-open."""
    if not getattr(cfg, "micro_1m_check_enabled", False):
        return True, ""
    dir_up = (direction or "").upper()
    try:
        raw = client.get_kline(market, interval="1", limit=16)
        if not raw or len(raw) < 6:
            return True, "1m_no_data"
        # ① 마지막 1M 봉 방향 — ★ [2026-06-20] body_min 노이즈 도지 면제(현물 전용 진입 0건 교정):
        #   직전 1M 몸통이 body_min% 미만(=노이즈 도지)이면 색깔 무관 통과. 명확한 역봉(|body|≥body_min)만 차단.
        #   기존엔 색깔(c<o)만 보고 -0.02% 도지도 LONG 차단 → 1m_candle_against 가 진입 0건의 한 축이었음.
        #   body_min=0(미마이그레이션)이면 |body|≥0 항상 참 = 종전 동작 100% 불변(하위호환).
        last = raw[-1]
        if len(last) >= 5:
            o, c = float(last[1]), float(last[4])
            body_min = float(getattr(cfg, "micro_1m_body_min_pct", 0.0) or 0.0)
            body_pct = abs(c - o) / o * 100.0 if o > 0 else 0.0
            if body_pct >= body_min:
                if dir_up == "LONG" and c < o:
                    return False, f"1m_candle_against(LONG 1M 음봉 o={o:.4f} c={c:.4f} body={body_pct:.3f}%≥{body_min})"
                if dir_up == "SHORT" and c > o:
                    return False, f"1m_candle_against(SHORT 1M 양봉 body={body_pct:.3f}%≥{body_min})"
        # ② 거래량 연속 감소(추진력 소진)
        n = int(getattr(cfg, "micro_1m_vol_decline_bars", 3))
        if len(raw) >= n + 1:
            vols = [float(r[5]) if len(r) >= 6 else 0 for r in raw[-(n + 1):]]
            if all(v > 0 for v in vols) and all(vols[i] > vols[i + 1] for i in range(len(vols) - 1)):
                return False, f"1m_vol_decline({n}봉 연속 감소)"
        # ③ RSI 극단(과열)
        closes = [float(r[4]) for r in raw if len(r) >= 5]
        if len(closes) >= 15:
            gains = sum(max(0, closes[i] - closes[i - 1]) for i in range(1, len(closes)))
            losses = sum(max(0, closes[i - 1] - closes[i]) for i in range(1, len(closes)))
            ag, al = gains / 14.0, losses / 14.0
            rsi = 100.0 - (100.0 / (1.0 + ag / al)) if al > 0 else 100.0
            long_max = float(getattr(cfg, "micro_1m_rsi_long_max", 70.0))
            short_min = float(getattr(cfg, "micro_1m_rsi_short_min", 30.0))
            if dir_up == "LONG" and rsi > long_max:
                return False, f"1m_rsi_overheat(LONG RSI={rsi:.1f}>{long_max})"
            if dir_up == "SHORT" and rsi < short_min:
                return False, f"1m_rsi_overheat(SHORT RSI={rsi:.1f}<{short_min})"
        return True, "1m_ok"
    except Exception as exc:
        logger.debug("[SPOT_GUARD] micro_1m fail-open: %s", exc)
        return True, "1m_error"


def check_momentum_reversal(client: Any, market: str, direction: str, cfg: Any) -> Tuple[bool, str]:
    """직전 5M 1~3봉 역행 차단 — "이미 다 움직인 후/떨어지는 칼" 진입 회피.
    원본 _compute_momentum_reversal_penalty(8096-8162) 의 *강한 역행*만 BLOCK 으로 이식
    (medium/누적 약역행 점수감점은 Phase 2 guard_score 로). fail-open."""
    if not getattr(cfg, "momentum_reversal_enabled", False):
        return True, ""
    dir_up = (direction or "").upper()
    if dir_up not in ("LONG", "SHORT"):
        return True, "no-dir"
    try:
        raw = client.get_kline(market, interval="5", limit=20)
        if not raw or len(raw) < 5:
            return True, "no-data"
        highs = [float(r[2]) for r in raw[-15:] if len(r) >= 5]
        lows = [float(r[3]) for r in raw[-15:] if len(r) >= 5]
        closes = [float(r[4]) for r in raw[-15:] if len(r) >= 5]
        lookback = max(1, min(int(getattr(cfg, "momentum_reversal_lookback_bars", 3)), 5))
        if len(closes) < lookback + 2:
            return True, "no-data"
        # ATR(true range, period 14) 인라인 — 선물 원본의 indicators.atr import 가 깨져 no-op 였음 → 견고하게 직접 계산.
        trs, pc = [], None
        for h, l, c in zip(highs, lows, closes):
            tr = (h - l) if pc is None else max(h - l, abs(h - pc), abs(l - pc))
            trs.append(tr)
            pc = c
        atr_val = sum(trs[-14:]) / min(len(trs), 14) if trs else 0.0
        if not atr_val or atr_val <= 0:
            return True, "no-atr"
        last_change = closes[-1] - closes[-2]
        adverse_1bar = -last_change if dir_up == "LONG" else last_change   # 양수 = 역행
        strong_thr = float(getattr(cfg, "momentum_reversal_strong_atr", 1.0)) * atr_val
        if adverse_1bar >= strong_thr:
            return False, f"strong_rev ({adverse_1bar / atr_val:+.1f}ATR 직전 역행)"
        # ★ [2026-06-20] cum{N}_rev 하드블록 제거(현물 전용 진입 0건 교정):
        #   선물은 momentum_reversal 을 *점수감점*(-20, _compute_momentum_reversal_penalty)으로만 쓰고
        #   하드블록 안 함. 이 함수 docstring 의도("누적 약역행은 guard_score 로")와도 일치.
        #   spot 포트가 누적역행까지 return False 차단해 눌림목 진입을 영영 막던 것 → 강한 1봉 역행만 BLOCK.
        return True, "mom_ok"
    except Exception as exc:
        logger.debug("[SPOT_GUARD] momentum_reversal fail-open: %s", exc)
        return True, "mom_error"


def check_raw_body(client: Any, market: str, direction: str, cfg: Any) -> Tuple[bool, str]:
    """직전 5M N봉 시가→종가 net(raw 에너지)이 진입 반대면 차단 — RSI/MACD 가공값 통과해도
    raw price action 이 반대인 자리 거름. 원본 focus_manager.py:11205-11246.
    ★보수: min_net_pct>0 기본(노이즈 net 무시·명확한 역에너지만 차단=전멸 방지). fail-open."""
    if not getattr(cfg, "raw_body_enabled", False):
        return True, ""
    lookback = max(1, int(getattr(cfg, "raw_body_lookback", 3)))
    min_net = float(getattr(cfg, "raw_body_min_net_pct", 0.05))
    try:
        raw = client.get_kline(market, interval="5", limit=lookback + 1)
        if not raw or len(raw) < lookback + 1:
            return True, "raw_no_data"
        recent = raw[-(lookback + 1):-1]   # 마지막(진행중) 제외한 직전 N완성봉 (oldest-first)
        if not recent:
            return True, "raw_no_data"
        ref = float(recent[-1][4]) or 0.0
        if ref <= 0:
            return True, "raw_no_data"
        net_pct = sum(float(b[4]) - float(b[1]) for b in recent) / ref * 100.0
        du = (direction or "").upper()
        if du == "LONG" and net_pct < -abs(min_net):
            return False, f"raw_body {lookback}봉 net={net_pct:+.2f}%(매도 에너지)→LONG 차단"
        if du == "SHORT" and net_pct > abs(min_net):
            return False, f"raw_body net={net_pct:+.2f}%(매수)→SHORT 차단"
        return True, "raw_ok"
    except Exception as exc:
        logger.debug("[SPOT_GUARD] raw_body fail-open: %s", exc)
        return True, "raw_error"


def check_momentum_deriv(client: Any, market: str, direction: str, cfg: Any) -> Tuple[bool, str]:
    """5M RSI/MACD 변화율(최근 N평균 − 그전 N평균)이 진입 반대 가속이면 차단 — "같은 RSI=50 이라도
    올라오는 중 vs 꺾이는 중" 구분. 원본 focus_manager.py:11264-11344.
    ★보수: require_both 기본 True(RSI+MACD 둘 다 반대일 때만 차단=전멸 방지). fail-open."""
    if not getattr(cfg, "momentum_deriv_enabled", False):
        return True, ""
    du = (direction or "").upper()
    if du not in ("LONG", "SHORT"):
        return True, "no-dir"
    lookback = max(2, int(getattr(cfg, "momentum_deriv_lookback", 5)))
    rsi_thr = float(getattr(cfg, "momentum_deriv_rsi_slope", 2.0))
    macd_thr = float(getattr(cfg, "momentum_deriv_macd_slope", 0.0))
    require_both = bool(getattr(cfg, "momentum_deriv_require_both", True))
    try:
        from app.strategy.indicators import rsi_series, macd_hist_series
        need = 14 + lookback * 2 + 5
        raw = client.get_kline(market, interval="5", limit=need)
        if not raw or len(raw) < need:
            return True, "no-data"
        closes = [float(r[4]) for r in raw if len(r) >= 5]   # oldest-first (현물 이미 정렬)
        rsi_vals = rsi_series(closes, 14)
        macd_hist = macd_hist_series(closes, 12, 26, 9)
        if not rsi_vals or len(rsi_vals) < lookback * 2:
            return True, "no-rsi"
        if not macd_hist or len(macd_hist) < lookback * 2:
            return True, "no-macd"
        rsi_delta = sum(rsi_vals[-lookback:]) / lookback - sum(rsi_vals[-2 * lookback:-lookback]) / lookback
        macd_delta = sum(macd_hist[-lookback:]) / lookback - sum(macd_hist[-2 * lookback:-lookback]) / lookback
        if du == "LONG":
            rsi_against = rsi_delta < -abs(rsi_thr)
            macd_against = macd_delta < -abs(macd_thr)
        else:
            rsi_against = rsi_delta > abs(rsi_thr)
            macd_against = macd_delta > abs(macd_thr)
        blocked = (rsi_against and macd_against) if require_both else (rsi_against or macd_against)
        if blocked:
            return False, (f"momentum_deriv {du} 반대(RSIΔ={rsi_delta:+.1f} MACDΔ={macd_delta:+.4f} "
                           f"{'AND' if require_both else 'OR'})")
        return True, "mderiv_ok"
    except Exception as exc:
        logger.debug("[SPOT_GUARD] momentum_deriv fail-open: %s", exc)
        return True, "mderiv_error"


def check_mtf_align(client: Any, market: str, direction: str, cfg: Any) -> Tuple[bool, str]:
    """MTF 최종 차단 — 상위/단기 TF 구조가 진입과 *명확히 반대*면 차단(현물엔 점수만 있고 차단 없었음).
    원본 focus_manager.py:_final_d1_alignment_check(2791)+_regime_align_override(2696) 의 핵심:
    "D1/30M/15M 하나라도 명확히 반대 = 거슬림 → 진입 차단"(SIDEWAYS 는 통과=단기 노이즈). long-only. fail-open.
    ※ D1('D') 은 거래소별 interval 지원 달라 기본 제외(분봉 TF만). 지원 시 cfg.mtf_align_tfs 에 추가."""
    if not getattr(cfg, "mtf_align_enabled", False):
        return True, ""
    du = (direction or "").upper()
    try:
        from app.strategy.greenpen.pa_detector import OHLCV
        from app.strategy.greenpen.market_structure import analyze_structure
        tfs = [t.strip() for t in str(getattr(cfg, "mtf_align_tfs", "240,30,15")).split(",") if t.strip()]
        for tf in tfs:
            raw = client.get_kline(market, interval=tf, limit=40)
            if not raw or len(raw) < 15:
                continue
            candles = [OHLCV(float(r[1]), float(r[2]), float(r[3]), float(r[4]))
                       for r in raw if len(r) >= 5]
            if len(candles) < 15:
                continue
            trend = str(analyze_structure(candles).trend.value).upper()
            if du == "LONG" and trend.startswith("DOWN"):
                return False, f"MTF {tf}=DOWNTREND vs LONG (상위TF 거슬림)"
            if du == "SHORT" and trend.startswith("UP"):
                return False, f"MTF {tf}=UPTREND vs SHORT"
        return True, "mtf_ok"
    except Exception as exc:
        logger.debug("[SPOT_GUARD] mtf_align fail-open: %s", exc)
        return True, "mtf_error"


def check_entry_expectation(client: Any, market: str, direction: str, price: float, atr: float, cfg: Any) -> Tuple[bool, str]:
    """진입 기대치 게이트 — reward(도달잠재) 부족 or risk(손실폭) 과대면 차단.
    ★ app/manager/entry_expectation.py:compute_entry_expectation *재사용*(exchange-neutral, 선물과 공유 유틸·본체 무손상).
    원본 게이트 focus_manager.py:16653-16689. long-only. fail-open."""
    if not getattr(cfg, "entry_expectation_enabled", False):
        return True, ""
    if price <= 0:
        return True, ""
    try:
        from app.manager.entry_expectation import compute_entry_expectation
        from app.strategy.greenpen.pa_detector import OHLCV
        tf = str(getattr(cfg, "primary_tf", "240"))
        raw = client.get_kline(market, interval=tf, limit=60)
        if not raw or len(raw) < 20:
            return True, "ee_no_data"
        candles = [OHLCV(float(r[1]), float(r[2]), float(r[3]), float(r[4]))
                   for r in raw if len(r) >= 5]
        if len(candles) < 20:
            return True, "ee_no_data"
        exp = compute_entry_expectation(direction, price, candles, atr or price * 0.02)
        min_rr = float(getattr(cfg, "entry_expectation_min_rr", 1.0))
        min_reward = float(getattr(cfg, "entry_expectation_min_reward_pct", 0.8))
        max_risk = float(getattr(cfg, "entry_expectation_max_risk_pct", 6.0))
        # ★ [2026-06-20] RR floor 이식(현물 전용 port 누락 교정): 선물 EE 게이트(focus_manager.py:16653~)는
        #   rr_ratio<min_rr 차단을 하는데 spot 포트가 reward/risk 만 검사하고 RR floor 를 빠뜨렸음.
        #   다른 게이트를 풀어 진입을 열 때 RR 나쁜 junk 가 새지 않게 품질 바닥을 지킴(명문대 모델).
        if exp.rr_ratio < min_rr:
            return False, f"ee_rr {exp.rr_ratio:.2f}<{min_rr} (RR 부족)"
        if exp.reward_pct < min_reward:
            return False, f"ee_reward {exp.reward_pct:.2f}%<{min_reward}% (도달 잠재 부족)"
        if exp.risk_pct > max_risk:
            return False, f"ee_risk {exp.risk_pct:.2f}%>{max_risk}% (손실폭 과대)"
        return True, f"ee_ok(rr={exp.rr_ratio:.2f})"
    except Exception as exc:
        logger.debug("[SPOT_GUARD] entry_expectation fail-open: %s", exc)
        return True, "ee_error"


def check_microtiming_5m(client: Any, market: str, direction: str, cfg: Any) -> Tuple[bool, str]:
    """5M 마이크로 타이밍 — RSI/MACD/BB *변곡* 3종 점수, 2/3 미만이면 이번 tick 보류(defer, 다음 재평가).
    "conv 100 이어도 변곡 자리 아니면 기다림". 원본 focus_manager.py:7911-8007. long-only. fail-open.
    ★ BLOCK 아닌 WAIT — 다음 스캔서 재평가(영구차단 아님)."""
    if not getattr(cfg, "microtiming_5m_enabled", False):
        return True, ""
    du = (direction or "").upper()
    if du not in ("LONG", "SHORT"):
        return True, "no-dir"
    try:
        from app.strategy.indicators import rsi_with_prev, macd_hist_pair, bollinger_bands
        raw = client.get_kline(market, interval="5", limit=30)
        if not raw or len(raw) < 27:
            return True, "mt5_short"   # fetch 부족 → 게이트 무효(안전 측)
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
        # LONG(현물): RSI 과매도→상승변곡 / MACD hist 음수→양전(또는 축소) / BB 하단권→회복
        if rsi_prev is not None and rsi_now is not None and rsi_prev <= rsi_thr and rsi_now > rsi_prev:
            rsi_s = 1
        if hist_prev is not None and hist_now is not None and hist_prev < 0 and (hist_now > 0 or hist_now > hist_prev):
            macd_s = 1
        if bb_prev_pct is not None and bb_now_pct is not None and bb_prev_pct < bb_low and bb_now_pct >= bb_rec:
            bb_s = 1
        total = rsi_s + macd_s + bb_s
        min_score = int(getattr(cfg, "microtiming_5m_min_score", 2))
        if total < min_score:
            return False, f"microtiming_5m {total}/3<{min_score} (rsi{rsi_s}/macd{macd_s}/bb{bb_s} 변곡 부족)"
        return True, f"mt5_ok({total}/3)"
    except Exception as exc:
        logger.debug("[SPOT_GUARD] microtiming_5m fail-open: %s", exc)
        return True, "mt5_error"
