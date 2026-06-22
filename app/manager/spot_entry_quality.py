# ============================================================
# Upbit FOCUS Entry Quality — 진입품질 게이트 (long_only, 격리·순수)
# ------------------------------------------------------------
# 부모님 진단(2026-06-16): Upbit 라이브 첫날 진입이 거침 — 천장/끝물 잡음.
#   병은 *진입*(천장 추격), 청산은 증상. 컷을 더하지 말고 진입 room 을 본다.
#   "headroom 을 페널티 아니라 게이트(room 없으면 진입 차단)" — feedback_bad_entry_not_fixed_by_cut.
#
# 순수 함수만 — I/O·상태 없음. 각 게이트는 config 로 독립 ON/OFF, 기본 OFF=0변화.
#   (DESIGN_upbit_v3_ribbon §②단계 진입품질 config 확장 — 라이브 복귀 게이트)
# ============================================================
from __future__ import annotations

from typing import Any, Dict, List, Tuple


def check_headroom(
    price: float,
    zones: List[Dict[str, Any]],
    *,
    min_headroom_pct: float,
) -> Tuple[bool, str]:
    """진입가 머리 위 가장 가까운 저항(RESISTANCE)까지의 여유(headroom) 게이트.

    여유 < min_headroom_pct → 천장 추격 = 진입 차단(게이트, 페널티 아님).
    머리 위 저항 없음(깨끗) → 통과. min_headroom_pct<=0 → 게이트 OFF(항상 통과).

    Args:
        price: 현재가(진입 예정가).
        zones: GreenPen 직렬화 zone 리스트 [{type, price_low, price_high, strength}, ...].
        min_headroom_pct: 요구 최소 여유 %. 0 이하면 게이트 비활성.

    Returns:
        (ok, reason). ok=False 면 차단.
    """
    if min_headroom_pct <= 0 or price <= 0:
        return True, "headroom:off"
    # 머리 위 저항 = price_low 가 현재가보다 위에 있는 RESISTANCE zone 중 가장 가까운 것
    overhead = [
        float(z.get("price_low", 0) or 0)
        for z in (zones or [])
        if str(z.get("type", "")).upper() == "RESISTANCE"
        and float(z.get("price_low", 0) or 0) > price
    ]
    if not overhead:
        return True, "headroom:clear_sky"
    nearest = min(overhead)
    headroom_pct = (nearest - price) / price * 100.0
    if headroom_pct < min_headroom_pct:
        return False, f"headroom_block:{headroom_pct:.2f}%<{min_headroom_pct:.2f}% (저항 {nearest:.4f})"
    return True, f"headroom_ok:{headroom_pct:.2f}%"


def check_overextension(
    last: float,
    hi24: float,
    lo24: float,
    move_pct: float,
    *,
    range_pos_pct: float,
    min_move_pct: float,
) -> Tuple[bool, str]:
    """끝물 추격 게이트 — 24H 범위 상단 + 큰 급등이면 진입 차단(소진된 추세 추격 방지).

    Bybit `_check_overextension` 이식(2026-06-07 부모). ★단, **ADX 면제 없음** —
    부모님 진단(feedback_bad_entry_not_fixed_by_cut): "ADX≥30 면제가 천장진입 누수의 공통 구멍".
    펌프는 ADX 폭발 → 거기서 면제되던 게 80+ 천장 진입을 다 통과시킴. → 면제 제거가 핵심 fix.

    LONG 전용(현물): pos = (last-lo)/(hi-lo) ≥ range_pos_pct  AND  |move| ≥ min_move_pct → 차단.
    range_pos_pct<=0 → 게이트 OFF. 변동 작으면(<min_move) 끝물 아님 → 통과.

    Returns:
        (ok, reason). ok=False 면 차단.
    """
    if range_pos_pct <= 0:
        return True, "overext:off"
    rng = hi24 - lo24
    if last <= 0 or rng <= 0:
        return True, "overext:no_data"
    if abs(move_pct) < min_move_pct:
        return True, f"overext:small_move({move_pct:.1f}%)"
    pos = (last - lo24) / rng   # 0=24H 저점, 1=24H 고점
    if pos >= range_pos_pct:
        return False, f"overext_block:pos{pos*100:.0f}%≥{range_pos_pct*100:.0f}% move{move_pct:.1f}%"
    return True, f"overext_ok:pos{pos*100:.0f}%"


def check_blowoff(
    move_pct: float,
    *,
    blowoff_move_pct: float,
    direction: str = "LONG",
) -> Tuple[bool, str]:
    """Blow-off 끝물 — 24H |변동| 극단(≥임계) + *추격* 방향이면 진입 차단.

    Bybit `_check_blowoff` 이식(2026-06-13 부모 #1). overext의 ADX 면제 구멍 보완분 —
    펌프는 ADX 폭발해 overext에서 면제되던 것을, 이건 *24H 이동폭 크기*로 직접 잡는다(ADX 무관).
    범위 위치(pos) 요구 없음 = 급등 후 눌려도(범위 상단 아님) 파라볼릭 추격 위험은 잡음.

    현물 long_only: LONG on +급등(chg>0) = 추격 → 차단. LONG on -급락(chg<0) = fade(저점 매수) →
    면제(다른 게이트가 처리). blowoff_move_pct<=0 → OFF.

    Returns:
        (ok, reason). ok=False 면 차단.
    """
    if blowoff_move_pct <= 0:
        return True, "blowoff:off"
    move = abs(move_pct)
    if move < blowoff_move_pct:
        return True, f"blowoff:below({move:.0f}%<{blowoff_move_pct:.0f}%)"
    chasing = (direction or "").upper() == "LONG" and move_pct > 0
    if not chasing:
        return True, f"blowoff:fade({move_pct:+.0f}%)"
    return False, f"blowoff_block:24h{move_pct:+.0f}%≥{blowoff_move_pct:.0f}% 추격"


def atr_floored_sl_distance(
    entry_price: float,
    pct_sl_distance: float,
    atr: float,
    *,
    atr_sl_floor_mult: float,
) -> float:
    """고정 %SL 거리에 ATR 바닥을 깐다 — 잔챙이 코인 1분 노이즈 즉사 방지.

    부모님 진단: "고정 1% SL 이 잔챙이 코인 1분 노이즈보다 좁아 0~2분 SL 연발".
    SL 거리 = max(고정 %거리, atr_sl_floor_mult × ATR). SL 을 *넓히는* 방향만(좁히지 않음).
    atr_sl_floor_mult<=0 → 비활성(고정 %거리 그대로).

    Returns:
        보정된 SL 거리(가격 단위). 호출자가 entry - dist 로 SL 가격 계산.
    """
    if atr_sl_floor_mult <= 0 or atr <= 0:
        return pct_sl_distance
    atr_floor = atr_sl_floor_mult * atr
    return max(pct_sl_distance, atr_floor)
