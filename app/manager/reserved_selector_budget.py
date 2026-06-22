"""reserved_selector_budget.py – Budget allocation helpers.

Extracted from reserved_selector.py (L2630-2984) without any logic changes.
Functions: _suggest_budget, _suggest_budget_dynamic
"""
from __future__ import annotations

import math
from typing import Dict, Optional

from app.manager.reserved_selector_utils import _clamp, _finalize_usdt_notional


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _suggest_budget(
    *,
    strategy: str,
    base_usdt: float,
    vol24_usdt: float,
    vol_median_usdt: float,
    min_order_usdt: float,
    max_budget_usdt: float,
    price: float,
    entry_qty_guard_on: bool,
    entry_max_qty: float,
    depth_factor: float,
    depth_ask_usdt: float,
    depth_bid_usdt: float,
    # 동적 예산 배분 파라미터 (선택)
    total_capital_usdt: float = 0.0,
    existing_markets_count: int = 0,
    spread_bps: float = 0.0,
    range_ratio_24h: float = 0.0,
    trend_score: float = 0.0,  # [2026-02-03] 추세 점수 (-10 ~ +10)
    ai_features: Optional[Dict[str, float]] = None,  # [2026-02-03] LIGHTNING용 volatility
) -> Optional[float]:
    """Compute a recommended per-market budget that respects qty/depth guards.

    동적 예산 계산:
    - total_capital_usdt > 0이면 총 자본 기반 슬롯 배분 사용
    - 유동성(vol24), 스프레드, 전략 특성 반영
    - 기존 마켓 수 고려하여 균형 배분
    - 변동성(range_ratio_24h) 및 단가(price) 기반 차등 배분
    """
    # 동적 예산 배분 모드
    if total_capital_usdt > 0 and existing_markets_count >= 0:
        return _suggest_budget_dynamic(
            strategy=strategy,
            total_capital_usdt=total_capital_usdt,
            existing_markets_count=existing_markets_count,
            vol24_usdt=vol24_usdt,
            vol_median_usdt=vol_median_usdt,
            spread_bps=spread_bps,
            price=price,
            min_order_usdt=min_order_usdt,
            max_budget_usdt=max_budget_usdt,
            entry_qty_guard_on=entry_qty_guard_on,
            entry_max_qty=entry_max_qty,
            depth_factor=depth_factor,
            depth_ask_usdt=depth_ask_usdt,
            depth_bid_usdt=depth_bid_usdt,
            range_ratio_24h=range_ratio_24h,
            trend_score=trend_score,  # [2026-02-03] 추세 전달
            ai_features=ai_features,  # [2026-02-03] AI 피처 전달
        )

    # 기존 로직 (base_usdt 기반)
    base = float(base_usdt)
    if base <= 0:
        return None

    # scale by liquidity (gentle)
    med = float(vol_median_usdt) if vol_median_usdt > 0 else float(vol24_usdt)
    mult = 1.0
    if med > 0 and vol24_usdt > 0:
        mult = math.sqrt(float(vol24_usdt) / float(med))

    # pingpong: tighter scaling to keep order sizes consistent
    if str(strategy).upper() == "PINGPONG":
        mult = _clamp(mult, 0.75, 1.45)
    else:
        mult = _clamp(mult, 0.70, 1.70)

    budget = float(base) * float(mult)
    budget = _clamp(budget, float(min_order_usdt), float(max_budget_usdt) if max_budget_usdt > 0 else budget)

    # qty guard: budget <= price * max_qty
    if entry_qty_guard_on and entry_max_qty > 0 and price > 0:
        max_by_qty = float(price) * float(entry_max_qty)
        budget = min(budget, max_by_qty)

    # depth guard: required_notional = budget * depth_factor <= min(depth)
    if depth_factor > 0 and depth_ask_usdt > 0 and depth_bid_usdt > 0:
        max_by_depth = min(depth_ask_usdt, depth_bid_usdt) / float(depth_factor)
        budget = min(budget, max_by_depth)

    return _finalize_usdt_notional(budget, float(min_order_usdt))


def _suggest_budget_dynamic(
    *,
    strategy: str,
    total_capital_usdt: float,
    existing_markets_count: int,
    vol24_usdt: float,
    vol_median_usdt: float,
    spread_bps: float,
    price: float,
    min_order_usdt: float,
    max_budget_usdt: float,
    entry_qty_guard_on: bool,
    entry_max_qty: float,
    depth_factor: float,
    depth_ask_usdt: float,
    depth_bid_usdt: float,
    range_ratio_24h: float = 0.0,
    trend_score: float = 0.0,  # [2026-02-03] 추세 점수 (-10 ~ +10)
    ai_features: Optional[Dict[str, float]] = None,  # [2026-02-03] LIGHTNING용 volatility
) -> Optional[float]:
    """동적 예산 계산 (총 자본 기반).

    [2026-02-03] 추세 반영 추가:
    - 상승 추세 (트렌드 > 3): 예산 +30%
    - 하락 추세 (트렌드 < -3): 예산 -30%
    - 횡보 (-3 ~ +3): 변화 없음

    계산 방식:
    1. 슬롯 예산 = 총 자본 / (기존 마켓 + 신규 1개)
    2. 유동성 스케일링 (sqrt 완화)
    3. 전략별 가중치
    4. 스프레드 페널티
    5. 변동성(range_ratio) 페널티
    6. 단가 기반 차등
    7. 추세 기반 조정 (NEW!)
    8. 대규모 자본 안전장치
    """
    if total_capital_usdt <= 0:
        return None

    # ai_features 기본값
    if ai_features is None:
        ai_features = {}

    # 1. 기본 슬롯 예산
    slot_count = max(1, existing_markets_count + 1)
    reserve_ratio = 0.05  # 5% 예비금
    available = total_capital_usdt * (1.0 - reserve_ratio)
    base_budget = available / slot_count

    # 2. 유동성 스케일링
    liq_factor = 1.0
    med = float(vol_median_usdt) if vol_median_usdt > 0 else 10_000_000.0  # 10M USDT 기준
    if vol24_usdt > 0:
        liq_factor = min(1.5, max(0.6, math.sqrt(vol24_usdt / med)))

    # 3. 전략별 가중치
    strategy_weights = {
        "PINGPONG": 1.0,
        "AUTOLOOP": 1.1,
        "LADDER": 1.4,    # DCA는 더 많은 자본 필요
        "LIGHTNING": 0.7,  # 빠른 진출입 → 적은 자본
        "GAZUA": 1.2,      # 장기 홀드
    }
    strat_weight = strategy_weights.get(str(strategy).upper(), 1.0)

    # 4. 스프레드 페널티
    spread_factor = 1.0
    if spread_bps > 15:
        spread_factor = max(0.7, 1.0 - (spread_bps - 15) / 80)

    # 5. 변동성 페널티 (range_ratio_24h 기반)
    #    - range_ratio 3% 이하: 1.2x (안정적 코인 보너스)
    #    - range_ratio 3~8%: 1.0x (보통)
    #    - range_ratio 8% 이상: 0.5~0.8x (고변동 페널티)
    volatility_factor = 1.0
    if range_ratio_24h > 0:
        range_pct = range_ratio_24h * 100.0  # 0.08 → 8%
        if range_pct <= 3.0:
            volatility_factor = 1.2  # 스테이블/저변동 보너스
        elif range_pct <= 8.0:
            volatility_factor = 1.0  # 보통
        else:
            # 8% 초과: 선형 감소 (8% → 1.0, 20% → 0.5)
            volatility_factor = max(0.5, 1.0 - (range_pct - 8.0) / 24.0)

    # 6. 단가 기반 차등 (USDT 가격 기준)
    #    [FIX 2026-01-23] 기존 코드는 USD 기준 주석인데 실제 입력은 USDT라서
    #    log10(price)가 거의 항상 양수 → min(1.0, ...)에 의해 대부분 1.0으로 포화.
    #    → USDT 기준 pivot(10,000원)을 두고 저가<1, 고가>1이 되도록 수정.
    #
    #    예시 (pivot=$10=log10(1)):
    #    - BTC $95K: log10≈4.98 → factor≈1.60 → 1.5로 클램프
    #    - ETH $3.2K: log10≈3.51 → factor≈1.38
    #    - 저가 $0.50: log10≈-0.30 → factor≈0.81
    price_factor = 1.0
    if price > 0:
        logp = math.log10(price + 0.0001)  # 0 방지
        pivot = 1.0  # log10(10) - $10 기준점
        price_factor = _clamp(1.0 + (logp - pivot) * 0.15, 0.4, 1.5)

    # 7. 최종 계산 (기본) - 추세 반영 제외 (나중에 적용)
    budget = base_budget * liq_factor * strat_weight * spread_factor * volatility_factor * price_factor

    # =========================================================
    # 9. 대규모 자본 안전장치 (슬리피지/호가 충격 방지)
    # =========================================================

    # 6-1. 일일 거래량 대비 상한 (vol24의 0.5% 이내)
    #      → 저가 코인 vol24=50M USDT이면 최대 25K USDT
    #      → 대부분 코인은 이보다 훨씬 낮음
    vol_ratio_limit = 0.005  # 0.5%
    if vol24_usdt > 0:
        max_by_vol = vol24_usdt * vol_ratio_limit
        budget = min(budget, max_by_vol)

    # 6-2. 호가 깊이 상한 (양방향 깊이의 20% 이내)
    #      → 호가창 전체를 먹지 않도록
    if depth_ask_usdt > 0 and depth_bid_usdt > 0:
        depth_limit_ratio = 0.20  # 호가 깊이의 20%
        min_depth = min(depth_ask_usdt, depth_bid_usdt)
        max_by_depth_safe = min_depth * depth_limit_ratio
        budget = min(budget, max_by_depth_safe)

    # 6-3. 저가 코인 보호 (단가 대비 최대 수량 제한)
    #      → 저가 코인에 대량 주문 → 슬리피지 위험
    #      → 최대 보유 수량을 일일 거래량의 1%로 제한
    if price > 0 and vol24_usdt > 0:
        # 일일 거래 수량 추정 (거래금액 / 가격)
        daily_vol_qty = vol24_usdt / price
        # 최대 보유 수량 = 일일 거래량의 1%
        max_qty_safe = daily_vol_qty * 0.01
        max_by_qty_safe = price * max_qty_safe
        budget = min(budget, max_by_qty_safe)

    # 6-4. 개별 코인 상한 (총 자본 비율) - price_factor 연동 동적 상한
    #      [FIX 2026-01-23] 기존 고정 10% 상한이 모든 코인을 동일 예산으로 수렴시킴.
    #      → 고가 코인은 상한을 조금 더 열고, 저가 코인은 더 조이도록 동적 조정.
    #      예시 (총자본 $2,000):
    #      - BTC (price_factor=1.5): 10%*1.5=15% → $300 상한
    #      - ETH (price_factor=1.4): 10%*1.4=14% → $280 상한
    #      - 저가 (price_factor=0.77): 10%*0.77=7.7% → $154 상한
    base_max_ratio = 0.10  # 기존 분산 정책 유지
    max_per_coin_ratio = _clamp(base_max_ratio * price_factor, 0.06, 0.20)
    max_by_total = total_capital_usdt * max_per_coin_ratio
    budget = min(budget, max_by_total)

    # =========================================================
    # =========================================================
    # 7. 기본 제약 적용
    # =========================================================
    min_b = max(0.0, float(min_order_usdt))
    max_b = max_budget_usdt if max_budget_usdt > 0 else 10000.0  # 기본 상한 ($10K)
    budget = _clamp(budget, min_b, max_b)

    # qty guard (기존)
    if entry_qty_guard_on and entry_max_qty > 0 and price > 0:
        max_by_qty = float(price) * float(entry_max_qty)
        budget = min(budget, max_by_qty)

    # depth guard (기존 - 더 보수적으로)
    if depth_factor > 0 and depth_ask_usdt > 0 and depth_bid_usdt > 0:
        max_by_depth = min(depth_ask_usdt, depth_bid_usdt) / float(depth_factor)
        budget = min(budget, max_by_depth)

    # =========================================================
    # [2026-02-03] 8. 추세 기반 최종 조정
    # =========================================================
    # 안전 장치 적용 후 추세 반영 (상한 내에서 조정)
    trend_factor = 1.0

    # CONTRARIAN은 역발상 전략이므로 로직 반대
    if strategy == "CONTRARIAN":
        # 벤치마크(BTC) 하락 시 역행 코인 매수
        # - 약한 하락 (-1 ~ -3): 역행 신뢰도 높음 → 정상 예산
        # - 강한 하락 (< -3): 추가 하락 리스크 → 예산 축소
        # - 폭락 (< -5): 극도로 위험 → 예산 최소화
        # - 상승 추세 (> 1): 역행 아님 → 예산 축소
        if abs(trend_score) > 0.1:
            if trend_score < -5.0:  # 폭락 (-15% 이상)
                trend_factor = 0.5  # -50% 예산
            elif trend_score < -3.0:  # 강한 하락 (-10% 이상)
                trend_factor = 0.7  # -30% 예산
            elif trend_score < -1.0:  # 약한 하락 (-3% 이상)
                trend_factor = 1.0  # 정상 (역행 최적 환경)
            elif trend_score > 1.0:  # 상승 추세
                trend_factor = 0.3  # -70% 예산 (역행 신호 신뢰도 낮음)

    # [2026-02-03] LADDER는 DCA 전략 → 하락=기회
    elif strategy == "LADDER":
        # 하락장에서 분할매수(DCA) 기회
        # - 폭락 (< -5): 극한 DCA 기회 → 예산 증가
        # - 강한 하락 (< -3): DCA 기회 → 예산 증가
        # - 약한 하락 (< -1): 정상
        # - 상승 추세 (> 3): DCA 부적합 → 예산 축소
        if abs(trend_score) > 0.1:
            if trend_score < -5.0:  # 폭락 (-15% 이상)
                trend_factor = 1.3  # +30% 예산 (극한 DCA 기회)
            elif trend_score < -3.0:  # 강한 하락 (-10% 이상)
                trend_factor = 1.2  # +20% 예산
            elif trend_score < -1.0:  # 약한 하락 (-3% 이상)
                trend_factor = 1.1  # +10% 예산
            elif trend_score > 3.0:  # 강한 상승 추세
                trend_factor = 0.7  # -30% 예산 (DCA 부적합)

    # [2026-02-03] GAZUA는 선별적 장기 보유 → 추세 무관 균등 배분
    elif strategy == "GAZUA":
        # 저평가 코인 발굴 후 장기 보유
        # - 추세에 상관없이 일정한 예산 유지
        # - 진입 시점: AI 확신 (0.75+) + 저평가 지표
        # - 보유 철학: 장기 묻어두기 (TP 25%, Grace 24h)
        trend_factor = 1.0  # 추세 무관 균등 배분

    # [2026-02-03] LIGHTNING은 변동성 기반 (추세 무관)
    elif strategy == "LIGHTNING":
        # 급등 가능성 = 변동성
        # - 극고변동 (> 5): 급등 기회 多 → 예산 증가
        # - 고변동 (> 3): 정상
        # - 저변동 (< 1.5): 급등 불가능 → 예산 축소
        volatility = ai_features.get("volatility", 2.0)
        if volatility > 5.0:  # 극고변동
            trend_factor = 1.3  # +30% 예산
        elif volatility > 3.0:  # 고변동
            trend_factor = 1.2  # +20% 예산
        elif volatility > 1.5:  # 중변동
            trend_factor = 1.1  # +10% 예산
        else:  # 저변동 (<1.5)
            trend_factor = 0.7  # -30% 예산 (부적합)

    # [FIX 2026-03-05] SNIPER 전략: 역추세(하락 반등) 전략 - 하락 = 기회, 상승 = 부적합
    elif strategy == "SNIPER":
        if abs(trend_score) > 0.1:
            if trend_score < -3.0:    # 강한 하락 = SNIPER 진입 기회
                trend_factor = 1.2   # +20% 예산
            elif trend_score < -1.0:  # 약한 하락 = 소폭 우대
                trend_factor = 1.1   # +10% 예산
            elif trend_score > 3.0:   # 강한 상승 = SNIPER 역추세 부적합
                trend_factor = 0.7   # -30% 예산
            elif trend_score > 1.0:   # 약한 상승 = 소폭 축소
                trend_factor = 0.85  # -15% 예산
    # 일반 전략: 상승 추세 우대
    elif abs(trend_score) > 0.1:
        if trend_score > 3.0:  # 강한 상승 추세
            trend_factor = 1.3  # +30%
        elif trend_score > 1.0:  # 약한 상승 추세
            trend_factor = 1.15  # +15%
        elif trend_score < -3.0:  # 강한 하락 추세
            trend_factor = 0.7  # -30%
        elif trend_score < -1.0:  # 약한 하락 추세
            trend_factor = 0.85  # -15%

    budget = budget * trend_factor

    # 추세 적용 후 다시 상한 체크
    budget = min(budget, max_b)
    budget = max(budget, min_b)

    return _finalize_usdt_notional(budget, float(min_order_usdt))
