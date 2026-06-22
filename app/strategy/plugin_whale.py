# Extracted from strategy_plugins.py — Phase 2 (file diet)
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from app.strategy import indicators
from app.strategy.strategy_base import Decision, StrategyPlugin

logger = logging.getLogger(__name__)

class NotImplementedPlugin(StrategyPlugin):
    """아직 port 되지 않은 전략을 위한 안전 플러그인(항상 HOLD)."""

    def __init__(self, name: str):
        self.name = name

    def decide(self, ctx: Any, price: float) -> Decision:
        return Decision(signal="hold", reason=f"{self.name}:not_implemented", meta={})

# ======================================================================
# WHALE Plugin
# 전략 핵심: 지인 인사이트 — "큰 구름 밑에 고래가 산다"
#
# 진입 (AND 조건):
#   1. 거래량 스파이크: 최근 3분봉 거래량 > N기간 평균 × vol_spike_ratio
#   2. 구름 위 2캔들:  직전 2개 3분봉 종가 모두 Ichimoku cloud_top 위
#   3. StochRSI 교차: %K가 %D를 방금 위로 크로스 (bullish crossover)
#
# 청산:
#   - 구름 아래 2캔들: 직전 2개 종가 모두 cloud_bottom 아래 → 즉시 매도
#   - TP/SL 안전망
# ======================================================================
class WhalePlugin(StrategyPlugin):
    """고래 등 타기 전략 — Ichimoku + StochRSI + 거래량 스파이크."""

    name: str = "WHALE"

    # 상태 키
    _ST_IDLE   = "IDLE"
    _ST_ACTIVE = "ACTIVE"

    def __init__(self) -> None:
        # 마켓별 3분봉 캔들 캐시 {market: (ts, candles)}
        self._candle_cache: Dict[str, Tuple[float, list]] = {}
        self._lock = threading.Lock()
        # 백그라운드 갱신 중인 마켓 집합 (중복 fetch 방지)
        self._fetching: set = set()

    # ------------------------------------------------------------------
    # 캔들 조회 (30초 캐시 + 비동기 백그라운드 갱신)
    # 캐시 만료 시 stale 데이터를 즉시 반환하고 백그라운드에서 갱신
    # → tick 블로킹 없음
    # ------------------------------------------------------------------
    def _get_candles(self, market: str, unit: int = 3, count: int = 80) -> list:
        now = time.time()
        with self._lock:
            cached = self._candle_cache.get(market)
            cache_age = (now - cached[0]) if cached else 9999.0
            if cached and cache_age < 30.0:
                return cached[1]  # 신선한 캐시
            stale = cached[1] if cached else None

        # 캐시 만료 — 백그라운드 갱신 + stale 즉시 반환 (tick 블로킹 방지)
        if market not in self._fetching:
            self._fetching.add(market)
            def _bg_fetch(_m=market, _u=unit, _c=count, _now=now):
                try:
                    from app.core.multi_timeframe_ai import fetch_candles
                    candles = fetch_candles(_m, unit=_u, count=_c)
                    if candles:
                        with self._lock:
                            self._candle_cache[_m] = (time.time(), candles)
                except (OSError, TypeError, ValueError, OverflowError) as e:
                    logger.warning("[WHALE] bg candle fetch failed %s: %s", _m, e, exc_info=True)
                finally:
                    self._fetching.discard(_m)
            threading.Thread(target=_bg_fetch, daemon=True).start()

        # stale 캐시 있으면 반환, 없으면(첫 호출) 동기 fetch
        if stale is not None:
            return stale
        try:
            from app.core.multi_timeframe_ai import fetch_candles
            candles = fetch_candles(market, unit=unit, count=count)
            if candles:
                with self._lock:
                    self._candle_cache[market] = (time.time(), candles)
            return candles or []
        except (OSError, TypeError, ValueError, OverflowError) as e:
            logger.warning("[WHALE] candle fetch failed %s: %s", market, e, exc_info=True)
            return []

    # ------------------------------------------------------------------
    # 상태 관리
    # ------------------------------------------------------------------
    def _get_state(self, ctx: Any) -> str:
        return str(getattr(ctx, "_whale_state", self._ST_IDLE))

    def _set_state(self, ctx: Any, state: str) -> None:
        ctx._whale_state = state

    def _reset(self, ctx: Any) -> None:
        ctx._whale_state = self._ST_IDLE
        ctx._whale_entry_price = 0.0

    # ------------------------------------------------------------------
    # 진입 신호 분석
    # ------------------------------------------------------------------
    def _check_entry(self, market: str, candles: list, params: Dict) -> Tuple[bool, str]:
        """네 가지 조건 모두 충족 시 True 반환.

        1. RSI 30 이하 (최근 N봉 중 확인) — 고래 축적 구간
        2. 두터운 구름 위 2캔들 돌파 — 고래 부상 신호
        3. 거래량 스파이크 — 매수세 확인
        4. StochRSI %K > %D 교차 — 모멘텀 확인
        """
        if len(candles) < 60:
            return False, f"candles too few: {len(candles)}"

        closes  = [float(c.get("trade_price") or c.get("close") or 0) for c in candles]
        highs   = [float(c.get("high_price")  or c.get("high")  or closes[i]) for i, c in enumerate(candles)]
        lows    = [float(c.get("low_price")   or c.get("low")   or closes[i]) for i, c in enumerate(candles)]
        volumes = [float(c.get("candle_acc_trade_volume") or c.get("volume") or 0) for c in candles]

        if not closes or closes[-1] <= 0:
            return False, "invalid close"

        # ── 1. RSI 30 이하 확인 (고래 축적 구간) ──────────────────
        rsi_len = int(params.get("rsi_period", 14))
        rsi_entry_max = float(params.get("rsi_entry_max", 30.0))
        rsi_entry_lookback = int(params.get("rsi_entry_lookback", 5))  # 최근 N봉 중 한 번이라도
        rsi_min_recent = 100.0
        for i in range(rsi_entry_lookback):
            end_idx = len(closes) - i
            if end_idx < rsi_len + 1:
                break
            r = indicators.rsi(closes[:end_idx], rsi_len)
            if r is not None:
                rsi_min_recent = min(rsi_min_recent, r)
        if rsi_min_recent > rsi_entry_max:
            return False, f"RSI 미달: min_recent={rsi_min_recent:.1f} > {rsi_entry_max:.0f}"

        # ── 2. 두터운 구름 위 2캔들 돌파 ─────────────────────────
        cloud = indicators.ichimoku_cloud(
            highs, lows, closes,
            tenkan=int(params.get("ichimoku_tenkan", 9)),
            kijun=int(params.get("ichimoku_kijun", 26)),
            senkou_b_period=int(params.get("ichimoku_senkou_b", 52)),
        )
        if cloud is None:
            return False, "ichimoku: 데이터 부족"

        # 구름 두께 확인 — 두터울수록 신뢰도 높음
        cloud_mid = (cloud["cloud_top"] + cloud["cloud_bottom"]) / 2.0
        cloud_min_thickness_pct = float(params.get("cloud_min_thickness_pct", 1.5))
        thickness_pct = 0.0
        if cloud_mid > 0:
            thickness_pct = (cloud["cloud_top"] - cloud["cloud_bottom"]) / cloud_mid * 100.0
        if thickness_pct < cloud_min_thickness_pct:
            return False, f"구름 너무 얇음: {thickness_pct:.2f}% < {cloud_min_thickness_pct}%"

        c1_above = closes[-2] > cloud["cloud_top"]
        c2_above = closes[-1] > cloud["cloud_top"]
        if not (c1_above and c2_above):
            return False, (
                f"구름 미돌파: c1={closes[-2]:.1f} c2={closes[-1]:.1f} "
                f"top={cloud['cloud_top']:.1f}"
            )

        # ── 3. 거래량 스파이크 ─────────────────────────────────────
        vol_lookback = int(params.get("vol_lookback", 20))
        vol_spike_ratio = float(params.get("vol_spike_ratio", 2.0))
        if len(volumes) < vol_lookback + 1:
            return False, "거래량 이력 부족"
        recent_vol = volumes[-1]
        avg_vol    = sum(volumes[-vol_lookback - 1: -1]) / vol_lookback
        if avg_vol <= 0 or recent_vol < avg_vol * vol_spike_ratio:
            return False, f"거래량 스파이크 없음: {recent_vol:.0f} / avg {avg_vol:.0f} (need x{vol_spike_ratio})"

        # ── 4. StochRSI %K > %D 교차 ──────────────────────────────
        srsi = indicators.stochastic_rsi(
            closes,
            rsi_period=int(params.get("stoch_rsi_period", 14)),
            stoch_period=14,
            k_smooth=int(params.get("stoch_k_smooth", 3)),
            d_smooth=int(params.get("stoch_d_smooth", 3)),
        )
        if srsi is None:
            return False, "StochRSI: 데이터 부족"

        if not srsi["crossover"]:
            return False, f"교차 없음: k={srsi['k']:.1f} d={srsi['d']:.1f}"

        return True, (
            f"RSI_min={rsi_min_recent:.1f} "
            f"cloud_two={thickness_pct:.1f}% "
            f"vol={recent_vol/avg_vol:.1f}x "
            f"k={srsi['k']:.1f}>d={srsi['d']:.1f}"
        )

    # ------------------------------------------------------------------
    # 청산 신호 분석
    # ------------------------------------------------------------------
    def _check_exit(self, candles: list, entry_price: float, price: float, params: Dict) -> Tuple[bool, str]:
        """청산 조건 (우선순위 순):
        1. SL — 손절 우선
        2. RSI ≥ 65 — 고래 이익 실현 구간 (나오기 시작)
        3. 구름 아래 2캔들 — 추세 반전 확인
        4. TP 안전망
        """
        tp_pct       = float(params.get("tp_pct", 2.0))
        sl_pct       = float(params.get("sl_pct", 3.0))
        rsi_exit_min = float(params.get("rsi_exit_min", 65.0))
        rsi_len      = int(params.get("rsi_period", 14))

        if entry_price > 0:
            pnl = (price - entry_price) / entry_price * 100.0
            if pnl <= -sl_pct:
                return True, f"SL {pnl:+.2f}%"

        if len(candles) < 60:
            return False, ""

        closes = [float(c.get("trade_price") or c.get("close") or 0) for c in candles]
        highs  = [float(c.get("high_price")  or c.get("high")  or closes[i]) for i, c in enumerate(candles)]
        lows   = [float(c.get("low_price")   or c.get("low")   or closes[i]) for i, c in enumerate(candles)]

        # ── RSI ≥ 65: 고래 이익실현 구간 → 나오기 시작 ─────────
        rsi_val = indicators.rsi(closes, rsi_len)
        if rsi_val is not None and rsi_val >= rsi_exit_min:
            pnl = (price - entry_price) / entry_price * 100.0 if entry_price > 0 else 0.0
            return True, f"RSI 이탈 {rsi_val:.1f}≥{rsi_exit_min:.0f} pnl={pnl:+.2f}%"

        # ── 구름 아래 2캔들: 추세 반전 ──────────────────────────
        cloud = indicators.ichimoku_cloud(
            highs, lows, closes,
            tenkan=int(params.get("ichimoku_tenkan", 9)),
            kijun=int(params.get("ichimoku_kijun", 26)),
            senkou_b_period=int(params.get("ichimoku_senkou_b", 52)),
        )
        if cloud is not None:
            c1_below = closes[-2] < cloud["cloud_bottom"]
            c2_below = closes[-1] < cloud["cloud_bottom"]
            if c1_below and c2_below:
                return True, (
                    f"2캔들 구름 아래: c1={closes[-2]:.1f} c2={closes[-1]:.1f} "
                    f"bottom={cloud['cloud_bottom']:.1f}"
                )

        # ── TP 안전망 ───────────────────────────────────────────
        if entry_price > 0:
            pnl = (price - entry_price) / entry_price * 100.0
            if pnl >= tp_pct:
                return True, f"TP {pnl:+.2f}%"

        return False, ""

    # ------------------------------------------------------------------
    # 메인 결정 로직
    # ------------------------------------------------------------------
    def decide(self, ctx: Any, price: float) -> Decision:
        market = str(getattr(ctx, "market", "") or "")
        params = dict(getattr(ctx, "params", {}) or {})

        # 파라미터 기본값
        candle_unit = int(params.get("candle_unit", 3))

        state = self._get_state(ctx)
        candles = self._get_candles(market, unit=candle_unit)

        # ── IDLE → 진입 체크 ──────────────────────────────────────
        if state == self._ST_IDLE:
            ok, reason = self._check_entry(market, candles, params)
            if ok:
                self._set_state(ctx, self._ST_ACTIVE)
                ctx._whale_entry_price = price
                logger.info("[WHALE] %s 진입 → %s", market, reason)
                return Decision(
                    signal="buy",
                    reason=f"WHALE_ENTRY: {reason}",
                    meta={"whale_entry_price": price, "reason": reason},
                )
            return Decision(signal="hold", reason=f"WHALE_WAIT: {reason}")

        # ── ACTIVE → 청산 체크 ────────────────────────────────────
        if state == self._ST_ACTIVE:
            entry_price = float(getattr(ctx, "_whale_entry_price", 0.0))
            ok, reason = self._check_exit(candles, entry_price, price, params)
            if ok:
                self._reset(ctx)
                logger.info("[WHALE] %s 청산 → %s", market, reason)
                return Decision(
                    signal="sell",
                    reason=f"WHALE_EXIT: {reason}",
                    meta={"whale_exit_reason": reason},
                )
            pnl = (price - entry_price) / entry_price * 100.0 if entry_price > 0 else 0.0
            return Decision(signal="hold", reason=f"WHALE_HOLD pnl={pnl:+.2f}%")

        # 안전 폴백
        self._reset(ctx)
        return Decision(signal="hold", reason="WHALE_RESET")

    # ------------------------------------------------------------------
    # 전체 시장 스캐너 — autopilot이 WHALE 슬롯 채울 때 호출
    # 모든 USDT 마켓을 3분봉으로 훑어 고래 조건 맞는 코인 반환
    # ------------------------------------------------------------------
    def scan_markets(
        self,
        market_list: List[str],
        params: Optional[Dict] = None,
        exclude: Optional[set] = None,
    ) -> List[Dict]:
        """고래 진입 조건을 충족하는 마켓 목록을 반환한다.

        Args:
            market_list: 스캔할 마켓 목록 (예: 전체 USDT 마켓)
            params: 전략 파라미터 (기본값 사용 시 None)
            exclude: 이미 사용 중인 마켓 (제외)

        Returns:
            [{"market": str, "reason": str, "score": float}, ...]
            score가 높을수록 신호 강도 높음
        """
        if params is None:
            params = {}
        if exclude is None:
            exclude = set()

        results = []
        for market in market_list:
            if market in exclude:
                continue
            try:
                candles = self._get_candles(market, unit=3, count=80)
                ok, reason = self._check_entry(market, candles, params)
                if ok:
                    # score: RSI가 낮을수록 + 구름 두꺼울수록 높은 점수
                    score = 1.0
                    try:
                        closes = [float(c.get("trade_price") or c.get("close") or 0) for c in candles]
                        r = indicators.rsi(closes, int(params.get("rsi_period", 14)))
                        if r is not None:
                            score += max(0.0, (30.0 - r) / 10.0)  # RSI 낮을수록 +점
                    except (KeyError, AttributeError, TypeError, ValueError) as exc:
                        logger.warning("[plugin_whale] %s: %s", 'score: RSI가 낮을수록 + 구름 두꺼울수록 높은 점수', exc, exc_info=True)
                    results.append({"market": market, "reason": reason, "score": score})
                    logger.info("[WHALE/SCAN] 🐋 신호 발견! %s — %s", market, reason)
            except (KeyError, AttributeError, TypeError, ValueError) as e:
                logger.warning("[WHALE/SCAN] %s 스캔 실패: %s", market, e, exc_info=True)

        results.sort(key=lambda x: x["score"], reverse=True)
        return results

