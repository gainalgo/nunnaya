# ============================================================
# Entry Expectation Calculator — 진입 기대치 메커니즘 (Phase 1)
# ------------------------------------------------------------
# 진입 시점에 "이 자리에서 어디까지 갈 수 있나"를 시장 구조로 정량화.
#   Reward 거리 = 진입가 ~ 다음 자연 도달가 (primary_tf(H1) S/R zone)
#   Risk 거리   = 진입가 ~ 무효화 가격 (직전 primary_tf(H1) swing low/high)
#   RR ratio    = Reward / Risk
#
# Pure functions — no state, no HyperSystem dependency. cycle_tp.py 스타일.
# 입력 캔들은 OHLCV (oldest-first). dict↔OHLCV 변환은 호출부 책임.
# ATR 은 인자로 받되, OHLCV 경로용 헬퍼 atr_from_ohlcv() 를 함께 제공한다
# (technical_indicators.calc_atr_from_candles 는 Bybit dict 전용이라 못 씀).
# ============================================================
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple

from app.strategy.greenpen.pa_detector import OHLCV
from app.strategy.greenpen.zone_engine import compute_zones, Zone, ZoneType
from app.strategy.greenpen.market_structure import analyze_structure


# ── ATR Helper (OHLCV 입력용) ───────────────────────────────

def atr_from_ohlcv(candles: List[OHLCV], period: int = 14) -> float:
    """OHLCV 리스트(oldest-first)에서 ATR(절대 가격 단위) 계산.

    표준 True Range 평균. technical_indicators.calc_atr_from_candles 의
    OHLCV 입력 버전 — 그쪽은 Bybit dict("high_price" 등) 전용이라
    OHLCV 경로(zone_engine·market_structure 와 같은 입력)에서 못 쓴다.

    Args:
        candles: OHLCV 리스트, oldest-first (candles[-1] 이 최신).
        period: ATR 기간 (기본 14). 캔들이 부족하면 가능한 만큼 사용.

    Returns:
        ATR 값 (가격 단위). 캔들 2개 미만이거나 유효 TR 0개면 0.0.
    """
    if len(candles) < 2:
        return 0.0
    n = min(period, len(candles) - 1)
    recent = candles[-(n + 1):]
    true_ranges: List[float] = []
    for i in range(1, len(recent)):
        h = recent[i].high
        lo = recent[i].low
        pc = recent[i - 1].close
        if h <= 0 or lo <= 0 or pc <= 0:
            continue
        true_ranges.append(max(h - lo, abs(h - pc), abs(lo - pc)))
    if not true_ranges:
        return 0.0
    return sum(true_ranges) / len(true_ranges)


# ── Data Types ──────────────────────────────────────────────

@dataclass
class EntryExpectation:
    """진입 기대치 산정 결과.

    reward_pct / risk_pct 는 진입가 대비 가격 이동 거리(%) — leverage 무관.
    rr_ratio 는 reward_pct / risk_pct.
    """
    reward_pct: float            # 진입가 → reward_target 거리 (%, 항상 ≥ 0)
    risk_pct: float              # 진입가 → risk_invalidation 거리 (%, 항상 ≥ 0)
    rr_ratio: float              # reward_pct / risk_pct (risk 0 이면 0.0)
    reward_target: float         # 도달 목표가 (절대가격, 0 = 산정 불가)
    risk_invalidation: float     # 무효화 가격 (절대가격, 0 = 산정 불가)
    reward_source: str           # "primary_zone" / "m15_obstacle" / "atr_fallback" / "none"
    risk_source: str             # "primary_swing" / "atr_fallback" / "none"
    note: str                    # 진단용 한 줄 설명

    @property
    def is_valid(self) -> bool:
        """reward·risk 둘 다 산정됐는지 — Gate(Phase 2) 통과 전제."""
        return self.reward_pct > 0.0 and self.risk_pct > 0.0


# ── Public API ──────────────────────────────────────────────

def compute_entry_expectation(
    direction: str,
    entry_price: float,
    primary_candles: List[OHLCV],
    atr_primary: float,
    *,
    m15_candles: Optional[List[OHLCV]] = None,
    m15_atr: Optional[float] = None,
    swing_lookback: int = 5,
    max_risk_pct: float = 0.15,
) -> EntryExpectation:
    """진입 기대치(Reward / Risk / RR)를 시장 구조 기반으로 산정.

    Args:
        direction: "LONG" 또는 "SHORT".
        entry_price: 진입(예정) 가격.
        primary_candles: primary_tf(H1) OHLCV 리스트 (oldest-first). reward zone + risk swing 산정용.
        atr_primary: primary_tf(H1) ATR(14). zone 폭 계산 + S/R 미발견 시 fallback 거리.
        m15_candles: 15m OHLCV 리스트 (oldest-first). reward 경로 중간 장애물 탐지용.
        m15_atr: 15m ATR(14). m15_candles 와 함께 있어야 장애물 탐지 작동.
        swing_lookback: swing point 확인용 좌우 캔들 수.
        max_risk_pct: risk_invalidation(무효화선)이 진입가 대비 이 비율 넘게
            멀면 swing 을 신뢰하지 않고 ATR fallback 사용 (기본 0.15 = 15%).

    Returns:
        EntryExpectation. reward 또는 risk 산정 불가 시 해당 _pct=0.0, source="none".
        입력이 깨졌으면(entry_price<=0, primary_candles<3) 전부 0 + is_valid=False.
    """
    d = direction.upper()
    is_long = d == "LONG"

    # 입력이 깨졌으면 빈 결과 — Gate(Phase 2)에서 is_valid=False 로 차단됨
    if entry_price <= 0 or len(primary_candles) < 3:
        return EntryExpectation(
            reward_pct=0.0, risk_pct=0.0, rr_ratio=0.0,
            reward_target=0.0, risk_invalidation=0.0,
            reward_source="none", risk_source="none",
            note="invalid input (entry_price<=0 or primary_candles<3)",
        )

    reward_target, reward_source = _compute_reward_target(
        is_long, entry_price, primary_candles, atr_primary, m15_candles, m15_atr,
    )
    risk_invalidation, risk_source = _compute_risk_invalidation(
        is_long, entry_price, primary_candles, atr_primary, swing_lookback, max_risk_pct,
    )

    reward_pct = (
        abs(reward_target - entry_price) / entry_price * 100.0
        if reward_target > 0 else 0.0
    )
    risk_pct = (
        abs(entry_price - risk_invalidation) / entry_price * 100.0
        if risk_invalidation > 0 else 0.0
    )
    rr_ratio = reward_pct / risk_pct if risk_pct > 0 else 0.0

    note = (
        f"{d} @ {entry_price:.6g} | "
        f"reward {reward_pct:.2f}% (~{reward_target:.6g}, {reward_source}) | "
        f"risk {risk_pct:.2f}% (~{risk_invalidation:.6g}, {risk_source}) | "
        f"RR {rr_ratio:.2f}"
    )

    return EntryExpectation(
        reward_pct=round(reward_pct, 4),
        risk_pct=round(risk_pct, 4),
        rr_ratio=round(rr_ratio, 4),
        reward_target=round(reward_target, 8),
        risk_invalidation=round(risk_invalidation, 8),
        reward_source=reward_source,
        risk_source=risk_source,
        note=note,
    )


# ── Reward Target ───────────────────────────────────────────

def _compute_reward_target(
    is_long: bool,
    entry_price: float,
    primary_candles: List[OHLCV],
    atr_primary: float,
    m15_candles: Optional[List[OHLCV]],
    m15_atr: Optional[float],
) -> Tuple[float, str]:
    """다음 자연 도달가 산정: primary_tf(H1) S/R zone → m15 경로 장애물 단축 → ATR fallback."""
    target = 0.0
    source = "none"

    # 1. primary_tf(H1) zone 기반 1차 목표 (compute_zones 는 atr<=0 이면 [] 반환)
    if atr_primary > 0:
        zones = compute_zones(primary_candles, atr_primary)
        zone = _nearest_directional_zone(zones, entry_price, is_long)
        if zone is not None:
            # zone 의 먼저 닿는 경계 — LONG 은 하단, SHORT 는 상단 (보수적)
            target = zone.price_low if is_long else zone.price_high
            source = "primary_zone"

    # 2. m15 경로 장애물 — 1차 목표보다 먼저 닿는 반대 구조가 있으면 목표 단축
    if target > 0 and m15_candles and m15_atr and m15_atr > 0 and len(m15_candles) >= 3:
        m15_zones = compute_zones(m15_candles, m15_atr)
        obstacle = _path_obstacle(m15_zones, entry_price, target, is_long)
        if obstacle is not None:
            target = obstacle
            source = "m15_obstacle"

    # 3. S/R 미발견 → ATR fallback (구조를 못 읽은 자리 = 보수적 1×ATR)
    if target <= 0 and atr_primary > 0:
        target = entry_price + atr_primary if is_long else entry_price - atr_primary
        source = "atr_fallback"

    # ATR fallback 이 음수가 되는 극단 케이스 방지 (SHORT, atr > entry_price)
    if target <= 0:
        return 0.0, "none"

    return target, source


def _nearest_directional_zone(
    zones: List[Zone],
    entry_price: float,
    is_long: bool,
) -> Optional[Zone]:
    """진입가 너머에서 가장 가까운 도달 목표 zone.

    LONG → 진입가 위의 RESISTANCE, SHORT → 진입가 아래의 SUPPORT.
    """
    best: Optional[Zone] = None
    best_dist = float("inf")
    for z in zones:
        mid = (z.price_low + z.price_high) / 2.0
        if is_long:
            if z.type != ZoneType.RESISTANCE or mid <= entry_price:
                continue
            dist = mid - entry_price
        else:
            if z.type != ZoneType.SUPPORT or mid >= entry_price:
                continue
            dist = entry_price - mid
        if dist < best_dist:
            best_dist = dist
            best = z
    return best


def _path_obstacle(
    zones: List[Zone],
    entry_price: float,
    reward_target: float,
    is_long: bool,
) -> Optional[float]:
    """진입가 ~ reward_target 경로 사이에서 먼저 닿는 반대 구조(장애물) 가격.

    LONG → 경로 중 RESISTANCE, SHORT → 경로 중 SUPPORT. 없으면 None.
    """
    best: Optional[float] = None
    best_dist = float("inf")
    for z in zones:
        if is_long:
            if z.type != ZoneType.RESISTANCE:
                continue
            level = z.price_low  # 먼저 닿는 경계
            if not (entry_price < level < reward_target):
                continue
            dist = level - entry_price
        else:
            if z.type != ZoneType.SUPPORT:
                continue
            level = z.price_high
            if not (reward_target < level < entry_price):
                continue
            dist = entry_price - level
        if dist < best_dist:
            best_dist = dist
            best = level
    return best


# ── Risk Invalidation ───────────────────────────────────────

def _compute_risk_invalidation(
    is_long: bool,
    entry_price: float,
    primary_candles: List[OHLCV],
    atr_primary: float,
    swing_lookback: int,
    max_risk_pct: float = 0.15,
) -> Tuple[float, str]:
    """무효화 가격 산정: 직전 primary_tf(H1) swing low/high → ATR fallback.

    swing 이 진입가에서 max_risk_pct(기본 15%) 넘게 멀면 신뢰하지 않는다.
    급등락한 코인은 진입가 근처에 swing 이 없어 아주 먼 옛날 극값을 잡는데
    (예: SHORT 인데 swing high 가 진입가 +117%), 그러면 SL 이 절벽이 되고
    RR 이 망가진다. 그 경우 swing 을 버리고 ATR fallback 으로 강등.
    """
    structure = analyze_structure(primary_candles, lookback=swing_lookback)
    invalidation = 0.0
    source = "none"

    if is_long:
        # 진입가 아래의 가장 가까운(=가장 높은) swing low
        candidates = [
            s.price for s in structure.swings
            if not s.is_high and 0 < s.price < entry_price
        ]
        if candidates:
            invalidation = max(candidates)
            source = "primary_swing"
    else:
        # 진입가 위의 가장 가까운(=가장 낮은) swing high
        candidates = [
            s.price for s in structure.swings
            if s.is_high and s.price > entry_price
        ]
        if candidates:
            invalidation = min(candidates)
            source = "primary_swing"

    # ★ swing 이 비정상적으로 멀면(진입가 대비 max_risk_pct 초과) 신뢰 불가 → 버림
    if invalidation > 0 and abs(entry_price - invalidation) / entry_price > max_risk_pct:
        invalidation = 0.0
        source = "none"

    # swing 미발견 또는 과도하게 멀어 폐기됨 → ATR fallback
    if invalidation <= 0 and atr_primary > 0:
        invalidation = entry_price - atr_primary if is_long else entry_price + atr_primary
        source = "atr_fallback"

    # ATR fallback 이 음수가 되는 극단 케이스 방지 (LONG, atr > entry_price)
    if invalidation <= 0:
        return 0.0, "none"

    return invalidation, source
