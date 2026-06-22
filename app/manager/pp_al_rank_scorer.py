# ============================================================
# File: app/manager/pp_al_rank_scorer.py
# Phase 2-C: Advanced rank_score for PINGPONG and AUTOLOOP
# ============================================================
"""
Enriches reserved queue items with multi-metric rank_score using
5m candle data for market microstructure analysis.

PINGPONG rank_score (tight spread, oscillation, depth focus):
  spread_tightness  30%  — 좁은 스프레드 선호
  oscillation_freq  25%  — 가격 진동 빈도 (횡보 적합)
  book_depth        20%  — 호가 두께
  trade_velocity    15%  — 거래 빈도
  vol_consistency   10%  — 거래량 안정성

AUTOLOOP rank_score (range, cycle clarity, trend focus):
  range_ratio       30%  — 고저 진폭 (넓을수록 유리)
  cycle_clarity     25%  — 지지/저항 반복 패턴 명확도
  vol_trend         20%  — 거래량 증가 추세
  swing_regularity  15%  — 변동 주기 규칙성
  mean_reversion    10%  — 평균 회귀 강도 (BB 기반)
"""

from __future__ import annotations

import math
import time
import logging
from typing import Any, Dict, List, Tuple

import requests

from app.core.constants import BYBIT_MARKET_KLINE, bybit_v5_rest_category, parse_bybit_list

logger = logging.getLogger(__name__)

# Candle cache: market -> (candles, fetch_ts)
_candle_cache: Dict[str, Tuple[List[Dict[str, Any]], float]] = {}
_CANDLE_CACHE_TTL = 300.0  # 5분
_STAGE2_LIMIT = 20

def _sf(x: Any, default: float = 0.0) -> float:
    try:
        v = float(x)
        return v if math.isfinite(v) else default
    except (TypeError, ValueError):
        logger.warning("[RankScorer] _sf: conversion failed for %r", x, exc_info=True)
        return default

# ────────────────────────────────────────────
# Candle Fetcher (5m, cached)
# ────────────────────────────────────────────
def _fetch_candles_5m(
    markets: List[str],
    *,
    count: int = 48,
    timeout: float = 4.0,
) -> Dict[str, List[Dict[str, Any]]]:
    from app.core.rate_limiter import rate_limiter

    now = time.time()
    result: Dict[str, List[Dict[str, Any]]] = {}
    to_fetch: List[str] = []

    for m in markets:
        cached = _candle_cache.get(m)
        if cached and (now - cached[1]) < _CANDLE_CACHE_TTL:
            result[m] = cached[0]
        else:
            to_fetch.append(m)

    if not to_fetch:
        return result

    if rate_limiter.is_banned():
        logger.debug("[RankScorer] REST API banned, using cache only")
        return result

    for m in to_fetch:
        try:
            _last_err = None
            for _attempt in range(2):
                try:
                    r = requests.get(
                        BYBIT_MARKET_KLINE,
                        params={"category": bybit_v5_rest_category(), "symbol": m, "interval": "5", "limit": count},
                        timeout=timeout,
                    )
                    r.raise_for_status()
                    _last_err = None
                    break
                except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as _e:
                    _last_err = _e
                    import time as _t; _t.sleep(1.0)
            if _last_err:
                logger.warning("[pp_al_rank_scorer] request failed for %s after 2 attempts: %s", m, _last_err)
                continue
            raw = parse_bybit_list(r.json())
            data = [{"opening_price": float(k[1]), "high_price": float(k[2]), "low_price": float(k[3]), "trade_price": float(k[4]), "candle_acc_trade_volume": float(k[5]), "candle_acc_trade_price": float(k[4]) * float(k[5]) if float(k[5]) > 0 else 0.0, "timestamp": int(k[0])} for k in raw if isinstance(k, (list, tuple)) and len(k) >= 6]
            if data:
                _candle_cache[m] = (data, now)
                result[m] = data
            rate_limiter.record_success()
        except requests.RequestException as e:
            logger.warning("[pp_al_rank_scorer] request failed for %s: %s", m, e)
            rate_limiter.handle_api_error(str(e))
            break
        except Exception as exc:
            logger.warning("[pp_al_rank_scorer] candle fetch failed for %s", m, exc_info=True)
            logger.warning("[pp_al_rank_scorer] %s: %s", 'pp_al_rank_scorer fallback', exc, exc_info=True)

    return result

# ────────────────────────────────────────────
# Metric Computations
# ────────────────────────────────────────────
def _oscillation_freq(closes: List[float]) -> float:
    """방향 전환 빈도 (0‑1). 높을수록 횡보 적합."""
    if len(closes) < 3:
        return 0.0
    changes = 0
    for i in range(2, len(closes)):
        d1 = closes[i - 1] - closes[i - 2]
        d2 = closes[i] - closes[i - 1]
        if (d1 > 0 and d2 < 0) or (d1 < 0 and d2 > 0):
            changes += 1
    return changes / max(1, len(closes) - 2)

def _vol_consistency(volumes: List[float]) -> float:
    """거래량 안정성 (0‑1). 높을수록 안정적."""
    if len(volumes) < 3:
        return 0.5
    mean_v = sum(volumes) / len(volumes)
    if mean_v <= 0:
        return 0.0
    std_v = (sum((v - mean_v) ** 2 for v in volumes) / len(volumes)) ** 0.5
    cv = std_v / mean_v
    return max(0.0, min(1.0, 1.0 - cv))

def _cycle_clarity(closes: List[float]) -> float:
    """반복 패턴 명확도 — 디트렌드 자기상관 (0‑1)."""
    n = len(closes)
    if n < 12:
        return 0.0
    sma_len = min(20, n // 2)
    sma_val = sum(closes[-sma_len:]) / sma_len
    detrended = [c - sma_val for c in closes]

    lag = max(3, n // 4)
    if lag >= n:
        return 0.0
    mean_d = sum(detrended) / n
    var_d = sum((d - mean_d) ** 2 for d in detrended)
    if var_d <= 0:
        return 0.0
    cov = sum(
        (detrended[i] - mean_d) * (detrended[i - lag] - mean_d)
        for i in range(lag, n)
    )
    return max(0.0, min(1.0, abs(cov / var_d)))

def _vol_trend(volumes: List[float]) -> float:
    """거래량 추세 — 선형 회귀 기울기 정규화 (0‑1)."""
    n = len(volumes)
    if n < 5:
        return 0.5
    x_mean = (n - 1) / 2.0
    y_mean = sum(volumes) / n
    if y_mean <= 0:
        return 0.5
    num = sum((i - x_mean) * (volumes[i] - y_mean) for i in range(n))
    den = sum((i - x_mean) ** 2 for i in range(n))
    if den <= 0:
        return 0.5
    slope = num / den
    rel_slope = slope / y_mean
    return max(0.0, min(1.0, 0.5 + rel_slope * 10.0))

def _swing_regularity(closes: List[float]) -> float:
    """변동 주기 규칙성 — 극값 간격 일관성 (0‑1)."""
    if len(closes) < 10:
        return 0.0
    extrema_idx: List[int] = []
    for i in range(1, len(closes) - 1):
        if (closes[i] > closes[i - 1] and closes[i] > closes[i + 1]) or \
           (closes[i] < closes[i - 1] and closes[i] < closes[i + 1]):
            extrema_idx.append(i)
    if len(extrema_idx) < 3:
        return 0.0
    intervals = [extrema_idx[j + 1] - extrema_idx[j] for j in range(len(extrema_idx) - 1)]
    mean_iv = sum(intervals) / len(intervals)
    if mean_iv <= 0:
        return 0.0
    std_iv = (sum((iv - mean_iv) ** 2 for iv in intervals) / len(intervals)) ** 0.5
    cv = std_iv / mean_iv
    return max(0.0, min(1.0, 1.0 - cv))

def _mean_reversion(closes: List[float]) -> float:
    """평균 회귀 강도 — 중심선 교차 빈도 (0‑1)."""
    if len(closes) < 20:
        return 0.0
    window = closes[-20:]
    mid = sum(window) / len(window)
    crossings = 0
    for i in range(1, len(closes)):
        if (closes[i - 1] < mid and closes[i] > mid) or \
           (closes[i - 1] > mid and closes[i] < mid):
            crossings += 1
    max_crossings = max(1, len(closes) // 2)
    return min(1.0, crossings / max_crossings)

# ────────────────────────────────────────────
# Percentile Normalization
# ────────────────────────────────────────────
def _percentile_normalize(values: List[float]) -> List[float]:
    """값 → 백분위 순위 (0‑1). 동점은 평균 순위 배정."""
    n = len(values)
    if n <= 1:
        return [0.5] * n
    sorted_idx = sorted(range(n), key=lambda i: values[i])
    raw_ranks = [0.0] * n
    for rank, idx in enumerate(sorted_idx):
        raw_ranks[idx] = float(rank)
    # 동점 처리: 같은 값이면 평균 순위
    from itertools import groupby
    groups: Dict[float, List[int]] = {}
    for i, v in enumerate(values):
        groups.setdefault(v, []).append(i)
    for indices in groups.values():
        if len(indices) > 1:
            avg_rank = sum(raw_ranks[i] for i in indices) / len(indices)
            for i in indices:
                raw_ranks[i] = avg_rank
    denom = max(1, n - 1)
    return [raw_ranks[i] / denom for i in range(n)]

# ────────────────────────────────────────────
# Candle Data Extraction
# ────────────────────────────────────────────
def _extract_candle_data(
    candles: List[Dict[str, Any]],
) -> Tuple[List[float], List[float], List[float], List[float]]:
    """Bybit 캔들(최신 먼저) → 시간순 close/high/low/volume."""
    ordered = list(reversed(candles))
    closes = [_sf(c.get("trade_price"), 0.0) for c in ordered if c.get("trade_price")]
    highs = [_sf(c.get("high_price"), 0.0) for c in ordered if c.get("high_price")]
    lows = [_sf(c.get("low_price"), 0.0) for c in ordered if c.get("low_price")]
    volumes = [_sf(c.get("candle_acc_trade_price"), 0.0) for c in ordered]
    return closes, highs, lows, volumes

# ────────────────────────────────────────────
# Strategy-Specific Rank Score Application
# ────────────────────────────────────────────
def _apply_pingpong_ranks(
    items: List[Dict[str, Any]],
    candle_map: Dict[str, List[Dict[str, Any]]],
) -> None:
    if not items:
        return

    raw_spread: List[float] = []
    raw_osc: List[float] = []
    raw_depth: List[float] = []
    raw_velocity: List[float] = []
    raw_vol_cons: List[float] = []

    for it in items:
        m = str(it.get("market") or "").upper()
        metrics = it.get("metrics") or {}

        spread = _sf(metrics.get("spread_bps"), 999.0)
        raw_spread.append(max(0.0, 1.0 - spread / 100.0))

        depth_min = min(
            _sf(metrics.get("depth_ask_usdt"), 0.0),
            _sf(metrics.get("depth_bid_usdt"), 0.0),
        )
        raw_depth.append(math.log1p(depth_min))

        v24 = _sf(metrics.get("vol24_usdt"), 0.0)
        raw_velocity.append(v24 / (24.0 * 60.0) if v24 > 0 else 0.0)

        candles = candle_map.get(m, [])
        if candles:
            closes, _, _, volumes = _extract_candle_data(candles)
            raw_osc.append(_oscillation_freq(closes))
            raw_vol_cons.append(_vol_consistency(volumes))
        else:
            raw_osc.append(0.0)
            raw_vol_cons.append(0.5)

    n_sp = _percentile_normalize(raw_spread)
    n_osc = _percentile_normalize(raw_osc)
    n_dep = _percentile_normalize(raw_depth)
    n_vel = _percentile_normalize(raw_velocity)
    n_vc = _percentile_normalize(raw_vol_cons)

    for i, it in enumerate(items):
        rank = (
            0.30 * n_sp[i]
            + 0.25 * n_osc[i]
            + 0.20 * n_dep[i]
            + 0.15 * n_vel[i]
            + 0.10 * n_vc[i]
        )
        it["rank_score"] = round(rank, 6)
        it["rank_metrics"] = {
            "spread_tightness": round(n_sp[i], 4),
            "oscillation_freq": round(n_osc[i], 4),
            "book_depth": round(n_dep[i], 4),
            "trade_velocity": round(n_vel[i], 4),
            "vol_consistency": round(n_vc[i], 4),
        }

def _apply_autoloop_ranks(
    items: List[Dict[str, Any]],
    candle_map: Dict[str, List[Dict[str, Any]]],
) -> None:
    if not items:
        return

    raw_range: List[float] = []
    raw_cycle: List[float] = []
    raw_vt: List[float] = []
    raw_swing: List[float] = []
    raw_mr: List[float] = []

    for it in items:
        m = str(it.get("market") or "").upper()
        candles = candle_map.get(m, [])

        if candles:
            closes, highs, lows, volumes = _extract_candle_data(candles)

            if highs and lows:
                max_h = max(highs)
                pos_lows = [l for l in lows if l > 0]
                min_l = min(pos_lows) if pos_lows else 1.0
                raw_range.append((max_h - min_l) / min_l if min_l > 0 else 0.0)
            else:
                rr = _sf((it.get("metrics") or {}).get("range_ratio_24h"), 0.0)
                raw_range.append(rr)

            raw_cycle.append(_cycle_clarity(closes))
            raw_vt.append(_vol_trend(volumes))
            raw_swing.append(_swing_regularity(closes))
            raw_mr.append(_mean_reversion(closes))
        else:
            rr = _sf((it.get("metrics") or {}).get("range_ratio_24h"), 0.0)
            raw_range.append(rr)
            raw_cycle.append(0.0)
            raw_vt.append(0.5)
            raw_swing.append(0.0)
            raw_mr.append(0.0)

    n_rng = _percentile_normalize(raw_range)
    n_cyc = _percentile_normalize(raw_cycle)
    n_vt = _percentile_normalize(raw_vt)
    n_sw = _percentile_normalize(raw_swing)
    n_mr = _percentile_normalize(raw_mr)

    for i, it in enumerate(items):
        rank = (
            0.30 * n_rng[i]
            + 0.25 * n_cyc[i]
            + 0.20 * n_vt[i]
            + 0.15 * n_sw[i]
            + 0.10 * n_mr[i]
        )
        it["rank_score"] = round(rank, 6)
        it["rank_metrics"] = {
            "range_ratio": round(n_rng[i], 4),
            "cycle_clarity": round(n_cyc[i], 4),
            "vol_trend": round(n_vt[i], 4),
            "swing_regularity": round(n_sw[i], 4),
            "mean_reversion": round(n_mr[i], 4),
        }

# ────────────────────────────────────────────
# Public API
# ────────────────────────────────────────────
def enrich_pp_al_rank_scores(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """PINGPONG/AUTOLOOP 아이템에 고급 rank_score 추가.

    autopilot_manager가 scan 후 reserved_queue.replace() 전에 호출.
    다른 전략 아이템은 그대로 통과.

    Two-Stage:
      Stage 1: build_reserved_candidates에서 이미 필터링됨
      Stage 2: 상위 후보에 대해 5m 캔들 fetch → 고급 메트릭 계산
    """
    pp_items: List[Dict[str, Any]] = []
    al_items: List[Dict[str, Any]] = []
    other_items: List[Dict[str, Any]] = []

    for it in items:
        strat = str(it.get("strategy") or it.get("recommended_strategy") or "").upper()
        if strat == "PINGPONG":
            pp_items.append(it)
        elif strat == "AUTOLOOP":
            al_items.append(it)
        else:
            other_items.append(it)

    pp_stage2 = pp_items[:_STAGE2_LIMIT]
    al_stage2 = al_items[:_STAGE2_LIMIT]

    markets_to_fetch: List[str] = []
    for it in pp_stage2 + al_stage2:
        m = str(it.get("market") or "").upper()
        if m and m not in markets_to_fetch:
            markets_to_fetch.append(m)

    candle_map: Dict[str, List[Dict[str, Any]]] = {}
    if markets_to_fetch:
        try:
            candle_map = _fetch_candles_5m(markets_to_fetch)
        except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as e:
            logger.warning(f"[RankScorer] Candle fetch failed: {e}")

    if pp_stage2:
        _apply_pingpong_ranks(pp_stage2, candle_map)
    if al_stage2:
        _apply_autoloop_ranks(al_stage2, candle_map)

    for it in pp_items[_STAGE2_LIMIT:]:
        it["rank_score"] = _sf(it.get("score"), 0.0)
    for it in al_items[_STAGE2_LIMIT:]:
        it["rank_score"] = _sf(it.get("score"), 0.0)

    candle_count = len(candle_map)
    if pp_stage2 or al_stage2:
        logger.info(
            f"[RankScorer] Enriched PP={len(pp_stage2)} AL={len(al_stage2)} "
            f"candles={candle_count}/{len(markets_to_fetch)}"
        )

    return pp_items + al_items + other_items
