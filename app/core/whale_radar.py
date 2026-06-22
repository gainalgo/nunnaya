"""WhaleRadar — 고래 탐지 엔진 🐋

고래 전조 패턴 + PA 백윅 시그널(GreenPen 이론) 통합 탐지기.
FOCUS 스캔 루프에서 60초마다 호출, 추가 API 콜 없이 기존 5M kline 재사용.

탐지 계층:
  Layer 1  Precursor   (거래량, 압축, 드리프트, 시험펌프)
  Layer 2  PA Pattern  (Pin, Engulf, 3-candle)
  Layer 3  Back Wick   (SIG 생존 확인)
  Layer 4  Zone        (S/R 존 검증)
  Layer 5  Classify    (A Flash / B Grind / C Hunt / D Rotation)

점수:
  >= 7  🐋🐋🐋 WHALE POD   conviction +3
  >= 5  🐋🐋   HIGH ALERT  conviction +2
  >= 3  🐋     WATCH       conviction +1 + 텔레그램
  <  3         Normal      no action

사용:
  from app.core.whale_radar import whale_radar
  sig = whale_radar.scan("ZECUSDT", klines_5m, zones=[...])
  if sig:
      conviction += sig.conviction_boost
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
#  Data Structures
# ─────────────────────────────────────────────

@dataclass
class WhaleSignal:
    """단일 고래 탐지 신호."""
    market: str
    timestamp: float
    score: int                  # 0-10
    direction: str              # "LONG" | "SHORT"
    pattern_type: str           # A_FLASH / B_GRIND / C_HUNT / D_ROTATION / UNKNOWN
    # ── 상세 ──
    precursors: Dict[str, Any] = field(default_factory=dict)
    pa_pattern: str = ""        # PIN_BUY / PIN_SELL / ENGULF_BUY / ENGULF_SELL / PAT3_BUY / PAT3_SELL
    back_wick_valid: bool = False
    back_wick_level: float = 0.0
    zone_valid: bool = False
    expected_move_pct: float = 0.0
    conviction_boost: int = 0   # 0, 1, 2, 3

    def to_dict(self) -> Dict:
        return asdict(self)


# ─────────────────────────────────────────────
#  Kline helpers
# ─────────────────────────────────────────────

def _parse_candle(raw: list) -> Dict:
    """Bybit kline → dict. raw=[ts_ms, o, h, l, c, vol, turnover]"""
    o, h, l, c, vol = float(raw[1]), float(raw[2]), float(raw[3]), float(raw[4]), float(raw[5])
    body = abs(c - o)
    rng = h - l if h > l else 1e-12
    return {
        "ts": int(raw[0]) / 1000,
        "open": o, "high": h, "low": l, "close": c,
        "vol": vol,
        "body": body,
        "range": rng,
        "body_pct": body / o * 100 if o > 0 else 0,
        "range_pct": rng / o * 100 if o > 0 else 0,
        "bullish": c >= o,
        "upper_wick": h - max(o, c),
        "lower_wick": min(o, c) - l,
    }


def _candles(raw_klines: list) -> List[Dict]:
    """raw klines → parsed candle list (oldest first). 중복 파싱 방지."""
    if not raw_klines:
        return []
    # 이미 dict이면 그대로
    if isinstance(raw_klines[0], dict) and "open" in raw_klines[0]:
        return raw_klines
    return [_parse_candle(r) for r in raw_klines]


# ─────────────────────────────────────────────
#  Layer 1 — Precursor Detection
# ─────────────────────────────────────────────

_WINDOW = 12          # 60분 = 5M × 12
_BASELINE = 100       # 평균 기준 100캔들
_VOL_BUILDUP_THR = 0.50   # 후반 거래량 50%+ 증가
_SQUEEZE_THR = 0.25        # 레인지 25%+ 압축
_DRIFT_THR = 0.8           # 방향 드리프트 0.8%+
_TEST_PUMP_MIN = 0.3       # 시험 펌프 최소 0.3%
_ACCUM_BIAS_MIN = 7        # 12캔들 중 7개+ 같은 방향


def _detect_precursors(candles: List[Dict]) -> Dict[str, Any]:
    """최근 60분(12캔들) 전조 분석. 기준선은 100캔들 평균."""
    result = {
        "accumulation": False, "accumulation_count": 0,
        "test_pump": False, "test_pump_count": 0, "test_pump_details": [],
        "vol_buildup": False, "vol_buildup_pct": 0.0,
        "squeeze": False, "squeeze_pct": 0.0,
        "drift": False, "drift_pct": 0.0,
        "direction_hint": "NEUTRAL",
        "score": 0,
    }
    if len(candles) < _WINDOW + 10:
        return result

    window = candles[-_WINDOW:]           # 최근 12캔들
    baseline = candles[-_BASELINE - _WINDOW:-_WINDOW] if len(candles) >= _BASELINE + _WINDOW else candles[:-_WINDOW]

    if not baseline:
        return result

    # ── 1. Accumulation Bias (매집 편향) ──
    bull_count = sum(1 for c in window if c["bullish"])
    bear_count = _WINDOW - bull_count
    if bull_count >= _ACCUM_BIAS_MIN:
        result["accumulation"] = True
        result["direction_hint"] = "LONG"
    elif bear_count >= _ACCUM_BIAS_MIN:
        result["accumulation"] = True
        result["direction_hint"] = "SHORT"
    result["accumulation_count"] = max(bull_count, bear_count)

    # ── 2. Micro Test Pump (시험 펌프) ──
    test_pumps = []
    for i in range(len(window) - 1):
        c = window[i]
        n = window[i + 1]
        # 양봉 시험: body가 크고 다음 캔들이 50%+ 되돌림
        if c["body_pct"] >= _TEST_PUMP_MIN:
            if c["bullish"] and not n["bullish"] and n["body"] > c["body"] * 0.3:
                test_pumps.append({"idx": i, "dir": "UP", "size_pct": c["body_pct"]})
            elif not c["bullish"] and n["bullish"] and n["body"] > c["body"] * 0.3:
                test_pumps.append({"idx": i, "dir": "DOWN", "size_pct": c["body_pct"]})
    result["test_pump"] = len(test_pumps) > 0
    result["test_pump_count"] = len(test_pumps)
    result["test_pump_details"] = test_pumps

    # ── 3. Volume Buildup (거래량 축적) ──
    avg_base_vol = sum(c["vol"] for c in baseline) / len(baseline) if baseline else 1
    first_half_vol = sum(c["vol"] for c in window[:6]) / 6
    second_half_vol = sum(c["vol"] for c in window[6:]) / 6
    if first_half_vol > 0:
        vol_change = (second_half_vol - first_half_vol) / first_half_vol
        result["vol_buildup_pct"] = round(vol_change * 100, 1)
        if vol_change >= _VOL_BUILDUP_THR:
            result["vol_buildup"] = True
    # 또는 전반적 거래량이 기준선 대비 2배+
    window_avg = sum(c["vol"] for c in window) / _WINDOW
    if avg_base_vol > 0 and window_avg / avg_base_vol >= 2.0:
        result["vol_buildup"] = True

    # ── 4. Range Squeeze (레인지 압축) ──
    recent_ranges = [c["range_pct"] for c in window[-6:]]
    baseline_ranges = [c["range_pct"] for c in baseline[-20:]] if len(baseline) >= 20 else [c["range_pct"] for c in baseline]
    if baseline_ranges:
        avg_recent = sum(recent_ranges) / len(recent_ranges)
        avg_baseline = sum(baseline_ranges) / len(baseline_ranges)
        if avg_baseline > 0:
            squeeze = 1 - (avg_recent / avg_baseline)
            result["squeeze_pct"] = round(squeeze * 100, 1)
            if squeeze >= _SQUEEZE_THR:
                result["squeeze"] = True

    # ── 5. Net Drift (방향 드리프트) ──
    if window[0]["close"] > 0:
        drift = (window[-1]["close"] - window[0]["close"]) / window[0]["close"] * 100
        result["drift_pct"] = round(drift, 3)
        if abs(drift) >= _DRIFT_THR:
            result["drift"] = True
            if drift > 0 and result["direction_hint"] == "NEUTRAL":
                result["direction_hint"] = "LONG"
            elif drift < 0 and result["direction_hint"] == "NEUTRAL":
                result["direction_hint"] = "SHORT"

    # ── Precursor Score (max 5) ──
    score = 0
    if result["accumulation"]:
        score += 2
    if result["test_pump"]:
        score += 2
    if result["vol_buildup"]:
        score += 1
    if result["squeeze"]:
        score += 1
    if result["drift"]:
        score += 1
    result["score"] = min(score, 5)

    return result


# ─────────────────────────────────────────────
#  Layer 2 — PA Pattern Detection
# ─────────────────────────────────────────────

_PIN_WICK_RATIO = 0.60      # 핀바: 꼬리가 전체 레인지의 60%+
_PIN_BODY_RATIO = 0.35      # 핀바: 몸통이 전체 레인지의 35% 이하
_ENGULF_BODY_RATIO = 1.2    # 장악형: 현재 몸통이 이전의 1.2배+


def _detect_pa_pattern(candles: List[Dict]) -> Dict[str, Any]:
    """최근 5캔들에서 PA 패턴 탐지.

    Returns: {pattern, direction, candle_idx, confirmation_at, back_wick_candle_idx}
    """
    result = {
        "pattern": "",           # PIN / ENGULF / PAT3
        "pa_name": "",           # PIN_BUY, ENGULF_SELL, etc.
        "direction": "",         # LONG / SHORT
        "signal_candle_idx": -1, # 시그널 캔들 위치 (끝에서 역순)
        "confirmation_at": 0,    # 확인 캔들 번호 (Pat1→2, Pat2→3, Pat3→4)
        "detected": False,
    }
    if len(candles) < 6:
        return result

    recent = candles[-5:]  # 최근 5캔들

    # ── Pat 1: Pin Bar (핀바) ──
    # 최근 2~4번째 캔들에서 핀바 검색 (맨 마지막은 아직 미완성일 수 있음)
    for offset in range(1, 4):  # candles[-2], candles[-3], candles[-4]
        if offset >= len(recent):
            break
        c = recent[-offset]
        rng = c["range"]
        if rng <= 0:
            continue

        lower_ratio = c["lower_wick"] / rng
        upper_ratio = c["upper_wick"] / rng
        body_ratio = c["body"] / rng

        # BUY Pin: 긴 아래꼬리 (지지선에서 반등)
        if lower_ratio >= _PIN_WICK_RATIO and body_ratio <= _PIN_BODY_RATIO and lower_ratio > upper_ratio * 2:
            result.update({
                "pattern": "PIN", "pa_name": "PIN_BUY", "direction": "LONG",
                "signal_candle_idx": offset, "confirmation_at": 2, "detected": True,
            })
            return result

        # SELL Pin: 긴 위꼬리 (저항선에서 하락)
        if upper_ratio >= _PIN_WICK_RATIO and body_ratio <= _PIN_BODY_RATIO and upper_ratio > lower_ratio * 2:
            result.update({
                "pattern": "PIN", "pa_name": "PIN_SELL", "direction": "SHORT",
                "signal_candle_idx": offset, "confirmation_at": 2, "detected": True,
            })
            return result

    # ── Pat 2: Engulfing (장악형) ──
    for offset in range(1, 4):
        if offset + 1 >= len(recent):
            break
        prev = recent[-(offset + 1)]
        curr = recent[-offset]

        if prev["body"] <= 0:
            continue

        # Bullish Engulfing
        if (not prev["bullish"] and curr["bullish"] and
                curr["body"] >= prev["body"] * _ENGULF_BODY_RATIO and
                curr["close"] > prev["open"] and curr["open"] <= prev["close"]):
            result.update({
                "pattern": "ENGULF", "pa_name": "ENGULF_BUY", "direction": "LONG",
                "signal_candle_idx": offset, "confirmation_at": 3, "detected": True,
            })
            return result

        # Bearish Engulfing
        if (prev["bullish"] and not curr["bullish"] and
                curr["body"] >= prev["body"] * _ENGULF_BODY_RATIO and
                curr["close"] < prev["open"] and curr["open"] >= prev["close"]):
            result.update({
                "pattern": "ENGULF", "pa_name": "ENGULF_SELL", "direction": "SHORT",
                "signal_candle_idx": offset, "confirmation_at": 3, "detected": True,
            })
            return result

    # ── Pat 3: Three-candle reversal (삼봉/모닝스타/이브닝스타) ──
    for offset in range(1, 3):
        if offset + 2 >= len(recent):
            break
        c1 = recent[-(offset + 2)]  # 첫째
        c2 = recent[-(offset + 1)]  # 가운데 (작은 몸통)
        c3 = recent[-offset]         # 셋째

        if c1["body"] <= 0 or c3["body"] <= 0:
            continue

        # 가운데 캔들이 작아야 함 (indecision)
        if c2["body"] > min(c1["body"], c3["body"]) * 0.5:
            continue

        # Morning Star (BUY): 음→작은→양, 셋째가 첫째의 50%+ 회복
        if (not c1["bullish"] and c3["bullish"] and
                c3["close"] > (c1["open"] + c1["close"]) / 2):
            result.update({
                "pattern": "PAT3", "pa_name": "PAT3_BUY", "direction": "LONG",
                "signal_candle_idx": offset, "confirmation_at": 4, "detected": True,
            })
            return result

        # Evening Star (SELL): 양→작은→음, 셋째가 첫째의 50%+ 하락
        if (c1["bullish"] and not c3["bullish"] and
                c3["close"] < (c1["open"] + c1["close"]) / 2):
            result.update({
                "pattern": "PAT3", "pa_name": "PAT3_SELL", "direction": "SHORT",
                "signal_candle_idx": offset, "confirmation_at": 4, "detected": True,
            })
            return result

    return result


# ─────────────────────────────────────────────
#  Layer 3 — Back Wick SIG Validation
# ─────────────────────────────────────────────

def _check_back_wick(candles: List[Dict], pa: Dict) -> Dict[str, Any]:
    """PA 패턴 후 백윅 생존 확인.

    백윅 = 확인 캔들의 꼬리. 후속 캔들이 이 레벨을 깨지 않으면 SIG 유효.
    """
    result = {"valid": False, "level": 0.0, "destroyed": False, "reason": ""}

    if not pa.get("detected"):
        result["reason"] = "no_pa"
        return result

    sig_idx = pa["signal_candle_idx"]
    conf_offset = pa["confirmation_at"]  # Pat1→2, Pat2→3, Pat3→4

    # 확인 캔들 = 시그널 캔들 뒤의 conf_offset-1 번째
    # recent[-sig_idx]가 시그널 → 확인 캔들은 recent[-sig_idx + (conf_offset - 1)]
    conf_candle_pos = sig_idx - (conf_offset - 1)

    if conf_candle_pos < 1:
        # 확인 캔들이 아직 안 나왔거나 마지막 캔들 → 검증 불가
        result["reason"] = "awaiting_confirmation"
        # 아직 확인 대기 중이면 일단 유효로 간주 (기회 놓치지 않기 위해)
        result["valid"] = True
        return result

    recent = candles[-5:]
    if conf_candle_pos >= len(recent):
        result["reason"] = "idx_out_of_range"
        return result

    conf = recent[-conf_candle_pos]
    direction = pa["direction"]

    # 백윅 레벨 결정
    if direction == "LONG":
        # BUY 시그널 → 확인 캔들의 아래꼬리 끝 = 백윅 레벨
        result["level"] = conf["low"]
        # 후속 캔들이 이 레벨 아래로 내려가면 파괴
        subsequent = recent[-conf_candle_pos + 1:] if conf_candle_pos > 1 else []
        for s in subsequent:
            if s["low"] < result["level"]:
                result["destroyed"] = True
                result["reason"] = "wick_broken_below"
                return result
    else:
        # SELL 시그널 → 확인 캔들의 위꼬리 끝 = 백윅 레벨
        result["level"] = conf["high"]
        subsequent = recent[-conf_candle_pos + 1:] if conf_candle_pos > 1 else []
        for s in subsequent:
            if s["high"] > result["level"]:
                result["destroyed"] = True
                result["reason"] = "wick_broken_above"
                return result

    result["valid"] = True
    result["reason"] = "wick_survived"
    return result


# ─────────────────────────────────────────────
#  Layer 4 — Zone Validation (간이)
# ─────────────────────────────────────────────

def _check_zone(candles: List[Dict], pa: Dict, zones: list | None) -> bool:
    """PA 신호가 S/R 존에서 발생했는지 확인.

    zones가 없으면 캔들 데이터에서 간이 S/R 계산.
    """
    if not pa.get("detected"):
        return False

    direction = pa["direction"]
    if not candles:
        return False

    current_price = candles[-1]["close"]

    # FOCUS zones가 있으면 활용
    if zones:
        for z in zones:
            ztype = z.get("type", "")
            low = float(z.get("low", 0))
            high = float(z.get("high", 0))
            if low <= current_price <= high:
                if direction == "LONG" and "support" in ztype.lower():
                    return True
                if direction == "SHORT" and "resistance" in ztype.lower():
                    return True
            # 가까운 존 (가격의 0.5% 이내)
            dist_pct = min(abs(current_price - low), abs(current_price - high)) / current_price * 100
            if dist_pct < 0.5:
                if direction == "LONG" and "support" in ztype.lower():
                    return True
                if direction == "SHORT" and "resistance" in ztype.lower():
                    return True

    # 간이 S/R: 최근 100캔들의 주요 고/저점
    if len(candles) >= 50:
        highs = sorted([c["high"] for c in candles[-100:]], reverse=True)
        lows = sorted([c["low"] for c in candles[-100:]])

        # 상위 5% 영역 = 저항, 하위 5% 영역 = 지지
        top5 = len(highs) // 20 or 1
        bot5 = len(lows) // 20 or 1
        resistance_zone = sum(highs[:top5]) / top5
        support_zone = sum(lows[:bot5]) / bot5

        price_range = resistance_zone - support_zone
        if price_range > 0:
            proximity = 0.03  # 가격 범위의 3% 이내
            if direction == "LONG" and (current_price - support_zone) / price_range < proximity * 3:
                return True
            if direction == "SHORT" and (resistance_zone - current_price) / price_range < proximity * 3:
                return True

    return False


# ─────────────────────────────────────────────
#  Layer 5 — Pattern Classification
# ─────────────────────────────────────────────

def _classify_pattern(precursors: Dict, pa: Dict) -> str:
    """패턴 유형 분류.

    A_FLASH:  거래량 급증 + 핀바 → 1캔들 폭발
    B_GRIND:  레인지 압축 + 매집 → 지속 펌프
    C_HUNT:   반전 위킹 → 청산 사냥
    D_ROTATION: multi-coin (scan_multiple에서 결정)
    """
    squeeze = precursors.get("squeeze", False)
    accum = precursors.get("accumulation", False)
    vol = precursors.get("vol_buildup", False)
    test = precursors.get("test_pump", False)
    pa_type = pa.get("pattern", "")

    # B_GRIND: 스퀴즈 + 매집 (더 크고 오래 감)
    if squeeze and accum:
        return "B_GRIND"

    # A_FLASH: 거래량 + 핀바 (빠르고 강력)
    if vol and pa_type == "PIN":
        return "A_FLASH"

    # A_FLASH: 거래량 + 시험펌프 (고래 유력)
    if vol and test:
        return "A_FLASH"

    # C_HUNT: 큰 위킹 후 즉시 반전 (아직 미구현 — 향후 OI 데이터 필요)
    # 현재는 큰 꼬리 + 작은 몸통으로 간이 추정
    if pa_type == "PIN" and precursors.get("squeeze_pct", 0) < 10:
        return "C_HUNT"

    # 기본값
    if precursors["score"] >= 3:
        return "B_GRIND" if squeeze else "A_FLASH"

    return "UNKNOWN"


# ─────────────────────────────────────────────
#  Score Calculation & Expected Move
# ─────────────────────────────────────────────

def _calculate_score(precursors: Dict, pa: Dict, back_wick: Dict, zone_valid: bool) -> int:
    """종합 점수 계산 (0-10)."""
    score = precursors.get("score", 0)  # max 5

    # PA Layer (max 3)
    if pa.get("detected"):
        score += 1
    if back_wick.get("valid"):
        score += 1
    if zone_valid:
        score += 1

    # Classification bonus handled by caller (D_ROTATION)
    return min(score, 10)


def _estimate_move(candles: List[Dict], pattern_type: str) -> float:
    """ATR 기반 기대 이동량 (%). TF별 런닝 사이클 근사."""
    if len(candles) < 20:
        return 0.0

    # 20캔들 ATR
    atr_sum = sum(c["range"] for c in candles[-20:])
    atr = atr_sum / 20
    price = candles[-1]["close"]
    if price <= 0:
        return 0.0

    atr_pct = atr / price * 100

    # 패턴별 배수 (PDF 기반)
    multipliers = {
        "A_FLASH": 3.0,     # ATR × 3 (빠른 폭발, 짧음)
        "B_GRIND": 6.0,     # ATR × 6 (느리지만 크게 감)
        "C_HUNT": 2.0,      # ATR × 2 (되돌림만)
        "D_ROTATION": 4.0,  # ATR × 4 (중간)
        "UNKNOWN": 2.0,
    }
    mult = multipliers.get(pattern_type, 2.0)
    return round(atr_pct * mult, 2)


def _score_to_boost(score: int) -> int:
    """점수 → conviction 부스트."""
    if score >= 7:
        return 3  # 🐋🐋🐋 WHALE POD
    if score >= 5:
        return 2  # 🐋🐋 HIGH ALERT
    if score >= 3:
        return 1  # 🐋 WATCH
    return 0


def _score_label(score: int) -> str:
    if score >= 7:
        return "🐋🐋🐋 WHALE POD"
    if score >= 5:
        return "🐋🐋 HIGH ALERT"
    if score >= 3:
        return "🐋 WATCH"
    return ""


# ─────────────────────────────────────────────
#  Main WhaleRadar Class
# ─────────────────────────────────────────────

_ALERT_COOLDOWN = 300  # 같은 코인 알림 5분 간격
_HISTORY_MAX = 200     # 히스토리 최대 보관


class WhaleRadar:
    """고래 탐지 메인 엔진."""

    def __init__(self):
        self._history: List[WhaleSignal] = []
        self._last_alert_ts: Dict[str, float] = {}  # market → last telegram ts
        self._active_alerts: Dict[str, WhaleSignal] = {}  # market → latest signal
        self._notify_fn = None  # 텔레그램 알림 함수 (외부 주입)

    def set_notify(self, fn):
        """텔레그램 알림 함수 주입. fn(message: str) → None"""
        self._notify_fn = fn

    def scan(self, market: str, klines_5m: list,
             zones: list | None = None) -> WhaleSignal | None:
        """단일 마켓 스캔. 5M kline 데이터 필요 (100+ 캔들 권장).

        Returns: WhaleSignal if score >= 3, else None.
        """
        candles = _candles(klines_5m)
        if len(candles) < 24:  # 최소 2시간 데이터
            return None

        # ── Layer 1: Precursor ──
        precursors = _detect_precursors(candles)

        # ── Layer 2: PA Pattern ──
        pa = _detect_pa_pattern(candles)

        # ── Layer 3: Back Wick ──
        back_wick = _check_back_wick(candles, pa)

        # ── Layer 4: Zone ──
        zone_valid = _check_zone(candles, pa, zones)

        # ── Score ──
        score = _calculate_score(precursors, pa, back_wick, zone_valid)

        if score < 3:
            # 점수 낮으면 active alert에서도 제거
            self._active_alerts.pop(market.upper(), None)
            return None

        # ── Direction 결정 ──
        direction = pa.get("direction", "") or precursors.get("direction_hint", "NEUTRAL")

        # ── Classification ──
        pattern_type = _classify_pattern(precursors, pa)

        # ── Expected Move ──
        expected_move = _estimate_move(candles, pattern_type)

        # ── Build Signal ──
        signal = WhaleSignal(
            market=market.upper(),
            timestamp=time.time(),
            score=score,
            direction=direction,
            pattern_type=pattern_type,
            precursors=precursors,
            pa_pattern=pa.get("pa_name", ""),
            back_wick_valid=back_wick.get("valid", False),
            back_wick_level=back_wick.get("level", 0.0),
            zone_valid=zone_valid,
            expected_move_pct=expected_move,
            conviction_boost=_score_to_boost(score),
        )

        # ── Record ──
        self._active_alerts[market.upper()] = signal
        self._history.append(signal)
        if len(self._history) > _HISTORY_MAX:
            self._history = self._history[-_HISTORY_MAX:]

        # ── Log ──
        label = _score_label(score)
        logger.info(
            "[WHALE] %s %s score=%d dir=%s type=%s pa=%s wick=%s zone=%s move=%.1f%% boost=+%d",
            label, market, score, direction, pattern_type,
            pa.get("pa_name", "-"), back_wick.get("valid"), zone_valid,
            expected_move, signal.conviction_boost,
        )

        # ── Telegram Alert ──
        self._send_alert(signal)

        return signal

    def scan_multiple(self, markets_klines: Dict[str, list],
                      zones_map: Dict[str, list] | None = None) -> Dict[str, WhaleSignal]:
        """여러 마켓 동시 스캔. Type D (Rotation) 감지 포함.

        Args:
            markets_klines: {market: klines_5m_list}
            zones_map: {market: zones_list} (optional)

        Returns: {market: WhaleSignal} for score >= 3 only.
        """
        results: Dict[str, WhaleSignal] = {}
        zones_map = zones_map or {}

        for market, klines in markets_klines.items():
            sig = self.scan(market, klines, zones_map.get(market))
            if sig:
                results[market] = sig

        # ── Type D: Rotation 감지 (3+ 코인이 동시에 score >= 3) ──
        if len(results) >= 3:
            for sig in results.values():
                if sig.score < 7:  # D_ROTATION bonus
                    sig.pattern_type = "D_ROTATION"
                    sig.score = min(sig.score + 1, 10)
                    sig.conviction_boost = _score_to_boost(sig.score)
            logger.warning("[WHALE] 🐋🐋🐋 ROTATION detected! %d coins: %s",
                          len(results), list(results.keys()))
            if self._notify_fn:
                coins = ", ".join(f"{m}({s.score})" for m, s in results.items())
                self._notify_fn(f"🐋🐋🐋 ROTATION DETECTED!\n{len(results)} coins active: {coins}")

        return results

    def get_active_alerts(self) -> Dict[str, Dict]:
        """현재 활성 알림 (대시보드용). 5분 이내 신호만."""
        now = time.time()
        active = {}
        for mk, sig in list(self._active_alerts.items()):
            if now - sig.timestamp < 300:
                active[mk] = sig.to_dict()
            else:
                del self._active_alerts[mk]
        return active

    def get_history(self, market: str | None = None, hours: float = 24) -> List[Dict]:
        """히스토리 조회."""
        cutoff = time.time() - hours * 3600
        result = []
        for sig in self._history:
            if sig.timestamp < cutoff:
                continue
            if market and sig.market != market.upper():
                continue
            result.append(sig.to_dict())
        return result

    def _send_alert(self, signal: WhaleSignal):
        """텔레그램 알림 (쿨다운 적용)."""
        if not self._notify_fn:
            return
        now = time.time()
        last = self._last_alert_ts.get(signal.market, 0)
        if now - last < _ALERT_COOLDOWN:
            return

        self._last_alert_ts[signal.market] = now
        label = _score_label(signal.score)
        msg_lines = [
            f"{label}",
            f"📍 {signal.market} | {signal.direction}",
            f"Score: {signal.score}/10 | Type: {signal.pattern_type}",
        ]
        if signal.pa_pattern:
            msg_lines.append(f"PA: {signal.pa_pattern} | Wick: {'✅' if signal.back_wick_valid else '❌'}")
        p = signal.precursors
        details = []
        if p.get("accumulation"):
            details.append(f"매집 {p['accumulation_count']}/12")
        if p.get("test_pump"):
            details.append(f"시험펌프 {p['test_pump_count']}회")
        if p.get("vol_buildup"):
            details.append(f"거래량↑{p['vol_buildup_pct']:.0f}%")
        if p.get("squeeze"):
            details.append(f"압축 {p['squeeze_pct']:.0f}%")
        if details:
            msg_lines.append(" | ".join(details))
        if signal.expected_move_pct > 0:
            msg_lines.append(f"기대 이동: ±{signal.expected_move_pct:.1f}%")
        msg_lines.append(f"Conviction boost: +{signal.conviction_boost}")

        self._notify_fn("\n".join(msg_lines))


# ─────────────────────────────────────────────
#  Singleton
# ─────────────────────────────────────────────

whale_radar = WhaleRadar()
