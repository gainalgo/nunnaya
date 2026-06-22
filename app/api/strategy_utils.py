# ============================================================
# File: app/api/strategy_utils.py
# Extracted from strategy_router.py — Phase 1-A (file diet)
#
# 공통 유틸리티: 캐시, 슬롯 관리, 파라미터 정규화, 경고 생성
# ============================================================

import threading
import math
from time import time as time_now
from typing import Dict, Any, List, Optional
from pydantic import BaseModel

import logging
logger = logging.getLogger(__name__)



# ============================================================
# Constants
# ============================================================
CACHE_TTL = 60  # seconds (increased for heavy market scan APIs)
SNIPER_MIN_TP_PCT = 1.2
SNIPER_MIN_SL_PCT = 2.5
MANUAL_OVERFLOW_MAX = 2  # 수동 주문 시 슬롯 초과 허용 한도

# ============================================================
# API Response Cache (Thread-safe, TTL-based)
# ============================================================
_cache: Dict[str, tuple] = {}
_cache_lock = threading.Lock()


def _get_cached(key: str, ttl: float = CACHE_TTL) -> Any:
    """Get cached data if not expired."""
    with _cache_lock:
        if key in _cache:
            ts, data = _cache[key]
            if time_now() - ts < ttl:
                return data
    return None


def _set_cached(key: str, data: Any) -> None:
    """Store data in cache with current timestamp."""
    with _cache_lock:
        _cache[key] = (time_now(), data)


def _build_cache_key(endpoint: str, **params) -> str:
    """Build a cache key from endpoint and sorted params."""
    param_str = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    return f"{endpoint}?{param_str}" if param_str else endpoint


# ============================================================
# Safe type conversion
# ============================================================
def _to_float(v: Any, default: float = 0.0) -> float:
    if v is None:
        return default
    try:
        x = float(v)
        if x != x:  # NaN
            return default
        return x
    except (TypeError, ValueError):
        logger.warning("strategy_utils._to_float failed: %r, default %s", v, default, exc_info=True)
        return default


# ============================================================
# Sniper TP/SL normalization
# ============================================================
def _clamp_sniper_tp_sl(tp_val: Any, sl_val: Any) -> tuple:
    """Normalize sniper TP/SL to enforced minimum floors."""
    try:
        tp = float(tp_val)
    except (TypeError, ValueError):
        logger.warning("strategy_utils._clamp_sniper_tp_sl L77 except", exc_info=True)
        tp = SNIPER_MIN_TP_PCT
    try:
        sl = abs(float(sl_val))
    except (TypeError, ValueError):
        logger.warning("strategy_utils._clamp_sniper_tp_sl L81 except", exc_info=True)
        sl = SNIPER_MIN_SL_PCT
    if tp < SNIPER_MIN_TP_PCT:
        tp = SNIPER_MIN_TP_PCT
    if sl < SNIPER_MIN_SL_PCT:
        sl = SNIPER_MIN_SL_PCT
    return round(tp, 4), round(sl, 4)


# ============================================================
# Sniper budget cap
# ============================================================
def _snipers_budget_cap_by_price(price_usdt: float) -> float:
    """Conservative cap for SNIPER(s) to avoid over-allocation on low-price coins."""
    p = _to_float(price_usdt, 0.0)
    if p <= 0:
        return 150.0
    if p < 100.0:
        return 100.0
    if p < 1000.0:
        return 150.0
    if p < 10.0:
        return 250.0
    return 400000.0


def _cap_snipers_budget(requested_budget: float, price_usdt: float) -> float:
    req = max(5.0, _to_float(requested_budget, 5.0))
    cap = _snipers_budget_cap_by_price(price_usdt)
    return float(min(req, cap))


# ============================================================
# Slot management (수동 주문 공통)
# ============================================================
def _count_strategy_active_slots(system: Any, strategy: str) -> int:
    """특정 전략의 현재 ACTIVE 슬롯 수를 카운트."""
    strategy_upper = str(strategy or "").strip().upper()
    count = 0
    try:
        contexts = getattr(system.coordinator, "contexts", {}) or {}
        for market, ctx in contexts.items():
            ctrls = getattr(ctx, "controls", {}) or {}
            strat = ctrls.get("strategy", {}) or {}
            if not bool(strat.get("enabled")):
                continue
            mode = str(strat.get("mode") or "").strip().upper()
            if mode in ("SNIPER(S)", "SNIPERS"):
                mode = "SNIPER"
            if mode == strategy_upper:
                # SNIPER(S) scope는 별도 카운트 — 일반 SNIPER만 셈
                if strategy_upper == "SNIPER":
                    params = strat.get("params", {}) or {}
                    prof = str(params.get("profile") or "").strip().upper()
                    src = str(params.get("source") or "").strip().lower()
                    if prof == "SNIPERS" and src == "precision_scope":
                        continue  # scope 슬롯은 _current_scope_slot_count()에서 별도 관리
                count += 1
    except (KeyError, AttributeError, TypeError, ValueError) as exc:
        logger.warning("[STRAT_UTIL] SNIPER(S) scope는 별도 카운트 — 일반 SNIPER만 셈: %s", exc, exc_info=True)
    return count


def _get_strategy_slot_target(system: Any, strategy: str) -> int:
    """전략별 슬롯 타겟 수 조회."""
    strategy_upper = str(strategy or "").strip().upper()
    attr_map = {
        "PINGPONG": "reserved_pingpong_n",
        "AUTOLOOP": "reserved_autoloop_n",
        "LADDER": "reserved_ladder_n",
        "LIGHTNING": "reserved_lightning_n",
        "GAZUA": "reserved_gazua_n",
        "CONTRARIAN": "reserved_contrarian_n",
        "SNIPER": "reserved_sniper_n",
    }
    attr = attr_map.get(strategy_upper)
    if not attr:
        return 0
    return max(0, int(getattr(system, attr, 0) or 0))


def _check_manual_overflow(system: Any, strategy: str, market: str) -> Dict[str, Any]:
    """수동 주문 시 슬롯 초과 여부 확인.

    Returns:
        {"allowed": bool, "current": int, "target": int, "overflow": int,
         "is_overflow": bool, "message": str}
    """
    strategy_upper = str(strategy or "").strip().upper()
    target = _get_strategy_slot_target(system, strategy_upper)
    current = _count_strategy_active_slots(system, strategy_upper)

    # 이미 해당 마켓이 같은 전략에 있으면 re-setup(갱신) → 항상 허용
    market_upper = str(market or "").strip().upper()
    try:
        ctx = getattr(system.coordinator, "contexts", {}).get(market_upper)
        if ctx:
            ctrls = getattr(ctx, "controls", {}) or {}
            strat = ctrls.get("strategy", {}) or {}
            if bool(strat.get("enabled")):
                mode = str(strat.get("mode") or "").strip().upper()
                if mode in ("SNIPER(S)", "SNIPERS"):
                    mode = "SNIPER"
                if mode == strategy_upper:
                    return {"allowed": True, "current": current, "target": target,
                            "overflow": 0, "is_overflow": False,
                            "message": f"Re-setup existing {strategy_upper} slot"}
    except (KeyError, AttributeError, TypeError, ValueError) as exc:
        logger.warning("[STRAT_UTIL] 이미 해당 마켓이 같은 전략에 있으면 re-setup(갱신) → 항상 허용: %s", exc, exc_info=True)

    if target <= 0:
        # 타겟 미설정 → 제한 없음
        return {"allowed": True, "current": current, "target": target,
                "overflow": 0, "is_overflow": False, "message": ""}

    overflow = max(0, current - target)
    if overflow >= MANUAL_OVERFLOW_MAX:
        return {
            "allowed": False, "current": current, "target": target,
            "overflow": overflow, "is_overflow": True,
            "message": (f"{strategy_upper} slot limit reached: "
                        f"{current}/{target} (+{overflow} overflow, max +{MANUAL_OVERFLOW_MAX})")
        }
    is_overflow = current >= target
    return {
        "allowed": True, "current": current, "target": target,
        "overflow": overflow + (1 if is_overflow else 0),
        "is_overflow": is_overflow,
        "message": (f"{strategy_upper} overflow +{overflow + 1}/{MANUAL_OVERFLOW_MAX}"
                    if is_overflow else ""),
    }


def _generate_coin_warnings(system: Any, market: str, strategy: str = "") -> List[Dict[str, Any]]:
    """수동 주문 시 해당 코인의 현재 상태에 대한 경고/주의 사항 생성.

    각 경고: {"level": "warn"|"info"|"caution", "code": str, "message": str}
    """
    warnings: List[Dict[str, Any]] = []
    market_upper = str(market or "").strip().upper()

    try:
        from app.core.hyper_price_store import price_store
        current_price = float(price_store.get_price(market_upper) or 0)
        if current_price <= 0:
            warnings.append({"level": "warn", "code": "no_price",
                             "message": "Current price unavailable"})
            return warnings

        # RSI
        try:
            from app.strategy import indicators
            ctx = getattr(system.coordinator, "contexts", {}).get(market_upper)
            history = list(getattr(ctx, "price_history", []) or []) if ctx else []
            if len(history) >= 15:
                rsi = indicators.rsi(history, 14)
                if rsi is not None:
                    if rsi > 70:
                        warnings.append({"level": "warn", "code": "rsi_overbought",
                                         "message": f"RSI {rsi:.1f} — overbought zone (>70)"})
                    elif rsi > 60:
                        warnings.append({"level": "caution", "code": "rsi_high",
                                         "message": f"RSI {rsi:.1f} — relatively high"})
                    elif rsi < 25:
                        warnings.append({"level": "info", "code": "rsi_deep_oversold",
                                         "message": f"RSI {rsi:.1f} — deep oversold (potential bounce)"})
        except (KeyError, AttributeError, TypeError, ValueError) as exc:
            logger.warning("[STRAT_UTIL] RSI: %s", exc, exc_info=True)

        # AI score — context brain에서 가져오기
        try:
            ctx = getattr(system.coordinator, "contexts", {}).get(market_upper)
            if ctx:
                brain = getattr(ctx, "current_ai", {}) or {}
                if isinstance(brain, dict):
                    brain = brain.get("brain", {}) or {}
                ai_score = float(brain.get("ai_prediction", 0) or 0)
                if ai_score < 0.3:
                    warnings.append({"level": "warn", "code": "ai_very_low",
                                     "message": f"AI confidence {ai_score:.0%} — very low"})
                elif ai_score < 0.5:
                    warnings.append({"level": "caution", "code": "ai_low",
                                     "message": f"AI confidence {ai_score:.0%} — below average"})
        except (KeyError, AttributeError, TypeError, ValueError) as exc:
            logger.warning("[STRAT_UTIL] AI score — context brain에서 가져오기: %s", exc, exc_info=True)

        # Near 24h high
        try:
            ctx = getattr(system.coordinator, "contexts", {}).get(market_upper)
            if ctx:
                high_24h = float(getattr(ctx, "high_price", 0) or 0)
                low_24h = float(getattr(ctx, "low_price", 0) or 0)
                if high_24h > 0 and low_24h > 0:
                    range_24h = high_24h - low_24h
                    if range_24h > 0:
                        pos_in_range = (current_price - low_24h) / range_24h * 100
                        if pos_in_range > 90:
                            warnings.append({"level": "warn", "code": "near_24h_high",
                                             "message": f"Price at {pos_in_range:.0f}% of 24h range — near daily high"})
                        elif pos_in_range > 75:
                            warnings.append({"level": "caution", "code": "upper_range",
                                             "message": f"Price at {pos_in_range:.0f}% of 24h range — upper quarter"})
                        elif pos_in_range < 15:
                            warnings.append({"level": "info", "code": "near_24h_low",
                                             "message": f"Price at {pos_in_range:.0f}% of 24h range — near daily low"})
        except (KeyError, AttributeError, TypeError, ValueError) as exc:
            logger.warning("[STRAT_UTIL] Near 24h high: %s", exc, exc_info=True)

        # 24h change rate
        try:
            ctx = getattr(system.coordinator, "contexts", {}).get(market_upper)
            if ctx:
                change = float(getattr(ctx, "signed_change_rate", 0) or 0) * 100
                if change > 10:
                    warnings.append({"level": "warn", "code": "pump_24h",
                                     "message": f"24h change +{change:.1f}% — already pumped significantly"})
                elif change < -10:
                    warnings.append({"level": "caution", "code": "dump_24h",
                                     "message": f"24h change {change:.1f}% — significant drop (falling knife risk)"})
        except (KeyError, AttributeError, TypeError, ValueError) as exc:
            logger.warning("[STRAT_UTIL] 24h change rate: %s", exc, exc_info=True)

        # Volume check
        try:
            ctx = getattr(system.coordinator, "contexts", {}).get(market_upper)
            if ctx:
                vol = float(getattr(ctx, "acc_trade_price_24h", 0) or 0)
                if vol > 0 and vol < 1_000_000:  # 1M USDT 미만
                    warnings.append({"level": "caution", "code": "low_volume",
                                     "message": f"24h volume {vol/1e6:.1f}M USDT — low liquidity"})
        except (KeyError, AttributeError, TypeError, ValueError) as exc:
            logger.warning("[STRAT_UTIL] Volume check: %s", exc, exc_info=True)

        # Cross-strategy conflict
        try:
            ctx = getattr(system.coordinator, "contexts", {}).get(market_upper)
            if ctx:
                ctrls = getattr(ctx, "controls", {}) or {}
                strat = ctrls.get("strategy", {}) or {}
                if bool(strat.get("enabled")):
                    existing_mode = str(strat.get("mode") or "").strip().upper()
                    strategy_upper = str(strategy or "").strip().upper()
                    if existing_mode and existing_mode != strategy_upper:
                        warnings.append({"level": "warn", "code": "cross_strategy",
                                         "message": f"Already running {existing_mode} strategy"})
        except (KeyError, AttributeError, TypeError, ValueError) as exc:
            logger.warning("[STRAT_UTIL] Cross-strategy conflict: %s", exc, exc_info=True)

    except Exception as exc:
        logger.warning("[STRAT_UTIL] _generate_coin_warnings: %s", exc, exc_info=True)

    return warnings


# ============================================================
# Candle fetching (short-lived cache)
# ============================================================
def _fetch_scope_candles_cached(
    market: str,
    *,
    unit: int,
    count: int,
    ttl: float = 10.0,
    force_refresh: bool = False,
) -> List[Dict[str, Any]]:
    """Short-lived candle cache for LONG/SHORT scope scan paths."""
    cache_key = _build_cache_key(
        "longshort/candles",
        market=str(market or "").strip().upper(),
        unit=int(unit),
        count=int(min(count, 200)),
    )
    if not force_refresh:
        cached = _get_cached(cache_key, ttl=ttl)
        if isinstance(cached, list):
            return list(cached)

    try:
        from app.core.multi_timeframe_ai import fetch_candles

        candles = list(fetch_candles(market, unit=unit, count=count) or [])
    except (ConnectionError, TimeoutError, OSError) as e:
        logger.warning("[strategy_utils] candle fetch network error for %s: %s", market, e)
        candles = []
    except (KeyError, AttributeError, TypeError):
        logger.warning("[strategy_utils] candle fetch failed for %s", market, exc_info=True)
        candles = []

    if candles:
        _set_cached(cache_key, candles)
    return list(candles)


# ============================================================
# Policy TP/SL sync
# ============================================================
def _sync_policy_tp_sl(ctx: Any, tp: Optional[float] = None, sl: Optional[float] = None) -> None:
    """Sync effective strategy TP/SL into engine policy."""
    try:
        if ctx is None:
            return

        if tp is None or sl is None:
            ctrls = getattr(ctx, "controls", None) or {}
            st = ctrls.get("strategy", {}) if isinstance(ctrls, dict) else {}
            sp = st.get("params", {}) if isinstance(st, dict) else {}
            if isinstance(sp, dict):
                if tp is None:
                    tp = sp.get("tp", sp.get("tp_pct"))
                if sl is None:
                    sl = sp.get("sl", sp.get("sl_pct"))
            mode = str(st.get("mode") or "").strip().upper() if isinstance(st, dict) else ""
        else:
            ctrls = getattr(ctx, "controls", None) or {}
            st = ctrls.get("strategy", {}) if isinstance(ctrls, dict) else {}
            mode = str(st.get("mode") or "").strip().upper() if isinstance(st, dict) else ""

        if tp is None and sl is None:
            return

        policy = getattr(ctx, "policy", None)
        if not isinstance(policy, dict):
            policy = {"name": "nunnaya", "params": {}}
        pparams = policy.get("params")
        if not isinstance(pparams, dict):
            pparams = {}

        if tp is not None:
            tp_num = float(tp)
            if tp_num < SNIPER_MIN_TP_PCT:
                tp_num = SNIPER_MIN_TP_PCT
            pparams["tp"] = tp_num
        if sl is not None:
            sl_num = float(sl)
            sl_norm = -abs(sl_num) if sl_num > 0 else sl_num
            if sl_norm > -SNIPER_MIN_SL_PCT:
                sl_norm = -SNIPER_MIN_SL_PCT
            if mode == "LADDER" and sl_norm > -5.0:
                sl_norm = -5.0
            pparams["sl"] = sl_norm

        policy["name"] = str(policy.get("name") or "nunnaya")
        policy["params"] = pparams
        if hasattr(ctx, "update_policy"):
            ctx.update_policy(policy)
        else:
            ctx.policy = policy
    except (KeyError, AttributeError, TypeError, ValueError) as exc:
        logger.warning("[STRAT_UTIL] strategy_utils fallback: %s", exc, exc_info=True)


# ============================================================
# Shared Pydantic models
# ============================================================
class StrategyStopRequest(BaseModel):
    market: str
    liquidate: bool = False
    delete: bool = False
    cleanup: bool = False
