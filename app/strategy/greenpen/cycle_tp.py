# ============================================================
# GreenPen Cycle-based TP/SL Calculator
# ------------------------------------------------------------
# Implements EP.5 from the Green Pen System:
#   - ATR-based dynamic TP1/TP2/SL (replaces fixed point targets)
#   - 2-stage take profit (TP1 partial → TP2 trailing)
#   - RR ratio enforcement (minimum 1:2.5)
#   - Position sizing based on SL distance
#
# Pure functions — no state, no HyperSystem dependency.
# ============================================================
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


# ── Data Types ──────────────────────────────────────────────

@dataclass
class CycleTargets:
    tp1: float          # Take Profit 1 (partial exit)
    tp2: float          # Take Profit 2 (full exit / trailing)
    sl: float           # Stop Loss
    rr_ratio: float     # Reward:Risk ratio (TP1/SL distance)
    atr_used: float     # ATR value used for calculation
    direction: str      # "LONG" or "SHORT"


@dataclass
class PartialExit:
    exit_pct: float     # percentage of position to close (e.g. 50)
    new_sl: float       # move SL to this price (typically entry = breakeven)
    reason: str


@dataclass
class PositionSizing:
    qty: float          # base quantity (before leverage)
    risk_usdt: float    # risk amount in USDT
    sl_distance: float  # SL distance in price
    leverage_qty: float # quantity after leverage


# ── Cycle Target Calculation ────────────────────────────────

def compute_cycle_targets(
    entry_price: float,
    direction: str,
    atr: float,
    *,
    tp1_mult: float = 2.5,
    tp2_mult: float = 5.0,
    sl_mult: float = 1.0,
    min_rr: float = 2.5,
    min_tp_distance_pct: float = 0.0,
) -> CycleTargets:
    """Compute ATR-based cycle TP1/TP2/SL targets.

    Green Pen cycle mapping (gold → crypto):
      H4: TP1 1,500pts / TP2 3,000pts → ATR × 2.5 / ATR × 5.0
      SL: ~600pts → ATR × 1.0

    Args:
        entry_price: position entry price.
        direction: "LONG" or "SHORT".
        atr: ATR(14) of the primary timeframe (e.g. H4).
        tp1_mult: TP1 = entry ± ATR × tp1_mult.
        tp2_mult: TP2 = entry ± ATR × tp2_mult.
        sl_mult: SL = entry ∓ ATR × sl_mult.
        min_rr: minimum RR ratio; adjusts SL tighter if needed.

    Returns:
        CycleTargets with TP1, TP2, SL, and RR ratio.
    """
    if atr <= 0 or entry_price <= 0:
        # Fallback: 2% TP1, 4% TP2, -1% SL
        fallback_atr = entry_price * 0.01
        atr = fallback_atr if fallback_atr > 0 else 1.0

    # ★ ATR 변동성 스케일링 (업비트 GreenPen 3단계 Step 1)
    # 변동성 큰 코인 → TP/SL 넓게, 작은 코인 → 좁게 (자동 적응)
    atr_pct = (atr / entry_price) * 100 if entry_price > 0 else 1.5
    vol_scale = max(0.7, min(1.8, atr_pct / 1.5))

    tp1_dist = atr * tp1_mult * vol_scale
    tp2_dist = atr * tp2_mult * vol_scale
    sl_dist = atr * sl_mult * vol_scale

    # ★ SL 최소값: 가격의 0.5% (금 같은 저변동 자산 보호)
    min_sl_dist = entry_price * 0.005
    if sl_dist < min_sl_dist:
        sl_dist = min_sl_dist

    # ★ TP 최소거리 (fee-guard, 2026-05-15 부모): 진입가×min_tp_distance_pct.
    #    저변동 코인(ATR 매우 작음)에서 cycle_tp가 수수료 왕복(0.11%)보다 가깝게 잡혀
    #    진입 직후 즉시 TP hit + 수수료 손실 패턴 방지. 0.0이면 비활성.
    if min_tp_distance_pct > 0.0 and entry_price > 0:
        min_tp_dist = entry_price * (min_tp_distance_pct / 100.0)
        if tp1_dist < min_tp_dist:
            tp1_dist = min_tp_dist
        # TP2는 항상 TP1보다 멀리
        if tp2_dist < tp1_dist * 1.5:
            tp2_dist = tp1_dist * 1.5

    # Enforce minimum RR ratio (SL을 넓히는 방향만 — 좁히기 금지)
    # 기존: sl_dist = tp1_dist / min_rr → SL 압축 → 즉사 원인
    # 수정: RR 부족하면 TP를 올리거나 그대로 유지 (SL 건드리지 않음)
    if sl_dist > 0:
        actual_rr = tp1_dist / sl_dist
        if actual_rr < min_rr:
            # TP1이 부족하면 TP1을 올림 (SL 압축 대신)
            tp1_dist = sl_dist * min_rr
            tp2_dist = max(tp2_dist, tp1_dist * 1.5)

    if direction.upper() == "LONG":
        tp1 = entry_price + tp1_dist
        tp2 = entry_price + tp2_dist
        sl = entry_price - sl_dist
    else:  # SHORT
        tp1 = entry_price - tp1_dist
        tp2 = entry_price - tp2_dist
        sl = entry_price + sl_dist

    rr = tp1_dist / max(sl_dist, 1e-12)

    return CycleTargets(
        tp1=round(tp1, 8),
        tp2=round(tp2, 8),
        sl=round(sl, 8),
        rr_ratio=round(rr, 2),
        atr_used=atr,
        direction=direction.upper(),
    )


# ── Partial Exit Logic ──────────────────────────────────────

def should_partial_exit(
    current_price: float,
    entry_price: float,
    direction: str,
    targets: CycleTargets,
    *,
    partial_pct: float = 50.0,
    already_partial: bool = False,
) -> Optional[PartialExit]:
    """Check if TP1 is reached for partial exit.

    Green Pen 2-stage exit:
      TP1 reached → close partial_pct% + move SL to entry (breakeven).
      Remaining position trails toward TP2.

    Returns:
        PartialExit instruction, or None if not triggered.
    """
    if already_partial:
        return None  # already did partial, don't repeat

    if direction.upper() == "LONG":
        if current_price >= targets.tp1:
            return PartialExit(
                exit_pct=partial_pct,
                new_sl=entry_price,  # breakeven
                reason=f"TP1 reached: {targets.tp1:.2f}",
            )
    else:  # SHORT
        if current_price <= targets.tp1:
            return PartialExit(
                exit_pct=partial_pct,
                new_sl=entry_price,  # breakeven
                reason=f"TP1 reached: {targets.tp1:.2f}",
            )

    return None


def should_full_exit(
    current_price: float,
    entry_price: float,
    direction: str,
    targets: CycleTargets,
    *,
    trailing_high: float = 0.0,
    trailing_low: float = 0.0,
    trailing_pct: float = 1.5,
) -> Optional[str]:
    """Check if SL or TP2 is hit, or trailing stop triggers.

    Returns:
        Reason string if should exit, None if hold.
    """
    d = direction.upper()

    # SL hit
    if d == "LONG" and current_price <= targets.sl:
        return f"SL hit: {current_price:.2f} <= {targets.sl:.2f}"
    if d == "SHORT" and current_price >= targets.sl:
        return f"SL hit: {current_price:.2f} >= {targets.sl:.2f}"

    # TP2 hit
    if d == "LONG" and current_price >= targets.tp2:
        return f"TP2 hit: {current_price:.2f} >= {targets.tp2:.2f}"
    if d == "SHORT" and current_price <= targets.tp2:
        return f"TP2 hit: {current_price:.2f} <= {targets.tp2:.2f}"

    # Trailing stop (only after TP1 / partial exit)
    if trailing_pct > 0:
        if d == "LONG" and trailing_high > 0:
            trail_price = trailing_high * (1.0 - trailing_pct / 100.0)
            if current_price <= trail_price:
                return f"Trailing stop: {current_price:.2f} <= trail {trail_price:.2f} (high={trailing_high:.2f})"

        if d == "SHORT" and trailing_low > 0:
            trail_price = trailing_low * (1.0 + trailing_pct / 100.0)
            if current_price >= trail_price:
                return f"Trailing stop: {current_price:.2f} >= trail {trail_price:.2f} (low={trailing_low:.2f})"

    return None


# ── Position Sizing (Green Pen 3M Money Management) ────────

def compute_position_size(
    budget_usdt: float,
    risk_pct: float,
    sl_distance: float,
    current_price: float,
    *,
    leverage: float = 1.0,
    max_daily_plans: int = 3,
) -> PositionSizing:
    """Green Pen lot size formula:
        plan_budget = budget / max_daily_plans
        risk_amount = plan_budget × risk_pct / 100
        qty = risk_amount / sl_distance
        leverage_qty = qty × leverage

    Args:
        budget_usdt: total FOCUS budget.
        risk_pct: % of plan budget to risk (e.g. 10 = 10%).
        sl_distance: SL distance in price (absolute).
        current_price: current market price.
        leverage: position leverage multiplier.
        max_daily_plans: max trades per day (Green Pen default: 3).
    """
    # max_daily_plans <= 10: 예산을 계획 수로 분배 (초록펜 3M 규율)
    # max_daily_plans > 10: 분배 없이 전체 예산 사용 (무제한 모드)
    if max_daily_plans <= 10:
        plan_budget = budget_usdt / max(max_daily_plans, 1)
    else:
        plan_budget = budget_usdt  # 무제한: 매 진입 시 전체 예산 기준
    risk_amount = plan_budget * (risk_pct / 100.0)

    if sl_distance <= 0 or current_price <= 0:
        return PositionSizing(qty=0, risk_usdt=0, sl_distance=0, leverage_qty=0)

    qty = risk_amount / sl_distance
    # leverage는 마진(증거금)만 줄임 — 포지션 크기를 키우면 안 됨
    # 수정 전: leverage_qty = qty * leverage (QTY 폭발 버그)
    leverage_qty = qty

    return PositionSizing(
        qty=round(qty, 8),
        risk_usdt=round(risk_amount, 2),
        sl_distance=round(sl_distance, 8),
        leverage_qty=round(leverage_qty, 8),
    )
