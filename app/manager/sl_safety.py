# ============================================================
# SL Safety — naked 포지션(서버측 SL 미확정) 보호 결정 (순수 함수)
# ------------------------------------------------------------
# "SL 낮은 부분 무사통과 → 청산" 방지. 서버 SL write-only 갭의 최후 안전망.
#   (DIAGNOSIS_bybit_naked_sl_liquidation_20260617.md)
# 순수 — I/O·상태 없음. 단위테스트 100%.
# ============================================================
from __future__ import annotations


def naked_sl_should_cut(
    direction: str,
    price: float,
    sl: float,
    *,
    buffer_pct: float,
    in_grace: bool,
) -> bool:
    """서버 SL 미확정(naked) 포지션을 지금 즉시 시장가 청산할지.

    - breach(진짜 SL 통과): grace 무관 *항상* 컷 (무사통과→청산 방지, 최우선).
    - near(버퍼 근접): 진입 직후 grace 동안은 미발동(슬리피지 노이즈), grace 후 선제 컷.
    호출자는 _tp_sl_confirmed=False 일 때만 이 함수를 호출(서버 SL 있으면 거래소가 막음).
    """
    if sl <= 0 or price <= 0:
        return False
    buf = max(0.0, buffer_pct) / 100.0
    d = (direction or "").upper()
    if d == "LONG":
        breach = price <= sl
        near = price <= sl * (1 + buf)
    else:  # SHORT
        breach = price >= sl
        near = price >= sl * (1 - buf)
    return breach or (near and not in_grace)
