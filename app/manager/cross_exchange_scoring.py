"""
Cross Exchange Scoring Module
거래소 간 시그널을 전략별 스코어링에 반영
"""
from typing import Dict, Any, Optional
import logging

logger = logging.getLogger(__name__)


def adjust_score_for_cross_exchange(
    base_score: float,
    strategy: str,
    coin: str,
    cross_signal: Optional[Any] = None
) -> Dict[str, Any]:
    """
    거래소 간 시그널을 반영하여 스코어 조정
    
    Args:
        base_score: 기본 스코어 (0.0 ~ 1.0)
        strategy: 전략 이름 ("PINGPONG", "LADDER", "LIGHTNING", etc.)
        coin: 코인 심볼 ("BTC", "ETH")
        cross_signal: CrossExchangeSignal 객체 (None이면 시그널 없음)
    
    Returns:
        {
            "adjusted_score": float,    # 조정된 스코어
            "multiplier": float,         # 적용된 배수
            "reasons": List[str],        # 조정 이유
            "liquidity_ok": bool,        # 유동성 충분 여부
            "arbitrage_risk": float,     # 차익거래 리스크 (0~1)
        }
    """
    
    if cross_signal is None:
        # 시그널 없으면 기본값 반환
        return {
            "adjusted_score": base_score,
            "multiplier": 1.0,
            "reasons": [],
            "liquidity_ok": True,
            "arbitrage_risk": 0.0,
        }
    
    multiplier = 1.0
    reasons = []
    
    # 데이터 나이 체크
    if cross_signal.data_age_sec > 30.0:
        # 30초 이상 오래된 데이터는 신뢰도 하락
        age_penalty = 0.95
        multiplier *= age_penalty
        reasons.append(f"stale_data_{cross_signal.data_age_sec:.0f}s")
    
    # === 전략별 조정 ===
    
    if strategy == "PINGPONG":
        # 단타 전략: 유동성 + 스프레드(차익) 매우 중요
        mult, reason = _adjust_pingpong(cross_signal)
        multiplier *= mult
        reasons.extend(reason)
    
    elif strategy == "AUTOLOOP":
        # 자동 조정 전략: 유동성 + 안정성 중요
        mult, reason = _adjust_autoloop(cross_signal)
        multiplier *= mult
        reasons.extend(reason)
    
    elif strategy == "LADDER":
        # 물타기 전략: 하락 시 차익 기회, 유동성 필수
        mult, reason = _adjust_ladder(cross_signal)
        multiplier *= mult
        reasons.extend(reason)
    
    elif strategy == "LIGHTNING":
        # 급등 전략: 선행지표 + 유동성 매우 중요
        mult, reason = _adjust_lightning(cross_signal)
        multiplier *= mult
        reasons.extend(reason)
    
    elif strategy == "GAZUA":
        # 장기 보유: 김치 프리미엄 + 유동성 중요
        mult, reason = _adjust_gazua(cross_signal)
        multiplier *= mult
        reasons.extend(reason)
    
    elif strategy == "CONTRARIAN":
        # 역발상: 차익거래 역이용, 김치 디스카운트 기회
        mult, reason = _adjust_contrarian(cross_signal)
        multiplier *= mult
        reasons.extend(reason)
    
    elif strategy == "SNIPER":
        # 저격 매수: 유동성 + 차익 안정성
        mult, reason = _adjust_sniper(cross_signal)
        multiplier *= mult
        reasons.extend(reason)
    
    # 유동성 체크 (모든 전략 공통)
    liquidity_ok = cross_signal.liquidity_score >= 0.3
    if cross_signal.liquidity_score < 0.3:
        multiplier *= 0.70  # 유동성 부족 시 30% 페널티 (완화)
        reasons.append(f"low_liquidity_{cross_signal.liquidity_score:.2f}")
    
    # 차익거래 리스크 계산
    arbitrage_risk = abs(cross_signal.arbitrage_pct) / 5.0  # 5% 기준
    arbitrage_risk = min(1.0, arbitrage_risk)
    
    adjusted_score = base_score * multiplier
    adjusted_score = max(0.0, min(1.0, adjusted_score))  # Clamp 0~1
    
    return {
        "adjusted_score": adjusted_score,
        "multiplier": multiplier,
        "reasons": reasons,
        "liquidity_ok": liquidity_ok,
        "arbitrage_risk": arbitrage_risk,
    }


# === 전략별 조정 함수 ===

def _adjust_pingpong(signal) -> tuple[float, list[str]]:
    """PINGPONG: 단타 전략"""
    mult = 1.0
    reasons = []
    
    # 1. 차익거래 스프레드가 크면 위험 (체결 안될 수 있음)
    if abs(signal.arbitrage_pct) > 0.5:
        mult *= 0.92  # -8% 페널티 (완화)
        reasons.append(f"wide_spread_{signal.arbitrage_pct:.2f}%")
    
    # 2. 유동성이 좋으면 보너스
    if signal.liquidity_score > 0.7:
        mult *= 1.05  # +5% 보너스 (완화)
        reasons.append("high_liquidity")
    
    # 3. 선행지표 무관 (단타는 즉각 반응)
    
    return mult, reasons


def _adjust_autoloop(signal) -> tuple[float, list[str]]:
    """AUTOLOOP: 자동 조정 전략"""
    SCALE = 0.75  # 최적 배율 (보수적, 효과 미미하므로 안전 우선)
    mult = 1.0
    reasons = []
    
    # 1. 안정적인 시장 선호 (차익 작음)
    if abs(signal.arbitrage_pct) < 0.3:
        mult *= (1.0 + 0.03 * SCALE)  # +3% 보너스 (완화)
        reasons.append("stable_market")
    
    # 2. 유동성 중요
    if signal.liquidity_score > 0.6:
        mult *= (1.0 + 0.05 * SCALE)  # +5% 보너스 (완화)
        reasons.append("good_liquidity")
    
    return mult, reasons


def _adjust_ladder(signal) -> tuple[float, list[str]]:
    """LADDER: 물타기 전략"""
    mult = 1.0
    reasons = []
    
    # 1. 하락 추세 + 차익 기회 = 물타기 기회
    if signal.leading_signal == "DOWN" and signal.leading_confidence > 0.6:
        # Binance가 하락 중 → Bybit도 하락 예상 → 물타기 준비
        mult *= 0.97  # -3% 페널티 (완화, 급락 리스크는 AI가 판단)
        reasons.append("downtrend_prepare")
    
    # 2. 김치 디스카운트(저평가) = 매수 기회
    if signal.kimchi_premium_pct < -1.0:  # -1% 이하
        mult *= 1.06  # +6% 보너스 (완화)
        reasons.append(f"kimchi_discount_{signal.kimchi_premium_pct:.1f}%")
    
    # 3. 유동성 매우 중요 (물타기는 대량 매수)
    if signal.liquidity_score < 0.4:
        mult *= 0.80  # -20% 페널티 (완화, 하지만 여전히 중요)
        reasons.append("low_liquidity_risk")
    
    return mult, reasons


def _adjust_lightning(signal) -> tuple[float, list[str]]:
    """LIGHTNING: 급등 전략"""
    mult = 1.0
    reasons = []
    
    # 1. 선행지표 중요 (BUT Confidence 기반 가중치)
    if signal.leading_signal == "UP" and signal.leading_confidence > 0.6:
        # Binance가 급등 중 → Bybit도 따라올 것
        # Confidence 기반: 0.6→+3%, 0.7→+6%, 0.8→+9%, 0.9→+12%
        confidence_bonus = (signal.leading_confidence - 0.5) * 0.24  # 최대 0.12
        mult *= (1.0 + confidence_bonus)
        reasons.append(f"leading_up_{signal.leading_change_pct:.1f}%_conf{signal.leading_confidence:.2f}")
    elif signal.leading_signal == "DOWN":
        # 하락 중이면 급등 전략 부적합
        mult *= 0.90  # -10% 페널티 (완화)
        reasons.append("leading_down_risk")
    
    # 2. 김치 프리미엄 과열 = 위험 신호
    if signal.kimchi_premium_pct > 3.0:
        mult *= 0.92  # -8% 페널티 (완화)
        reasons.append(f"overheated_{signal.kimchi_premium_pct:.1f}%")
    
    # 3. 유동성 필수 (급등은 빠른 진입/탈출)
    if signal.liquidity_score > 0.7:
        mult *= 1.08  # +8% 보너스 (완화)
        reasons.append("high_liquidity_burst")
    
    return mult, reasons


def _adjust_gazua(signal) -> tuple[float, list[str]]:
    """GAZUA: 장기 보유 전략"""
    SCALE = 3.0  # 최적 배율 (공격적, 분리도 +5.1%p)
    mult = 1.0
    reasons = []
    
    # 1. 김치 프리미엄 정상 범위 선호
    if -1.0 < signal.kimchi_premium_pct < 2.0:
        mult *= (1.0 + 0.03 * SCALE)  # +3% 보너스 (완화)
        reasons.append("normal_premium")
    elif signal.kimchi_premium_pct > 5.0:
        # 너무 과열되면 조정 리스크
        mult *= (1.0 - 0.06 * SCALE)  # -6% 페널티 (완화)
        reasons.append("overheated_risk")
    
    # 2. 선행지표 상승 = 좋은 신호 (BUT Confidence 기반)
    if signal.leading_signal == "UP" and signal.leading_confidence > 0.6:
        confidence_bonus = (signal.leading_confidence - 0.5) * 0.12 * SCALE  # 최대 +6%
        mult *= (1.0 + confidence_bonus)
        reasons.append(f"uptrend_conf{signal.leading_confidence:.2f}")
    
    # 3. 유동성 (장기 보유는 상대적으로 덜 중요)
    if signal.liquidity_score > 0.5:
        mult *= (1.0 + 0.02 * SCALE)  # +2% 보너스 (완화)
        reasons.append("adequate_liquidity")
    
    return mult, reasons


def _adjust_contrarian(signal) -> tuple[float, list[str]]:
    """CONTRARIAN: 역발상 전략"""
    mult = 1.0
    reasons = []
    
    # 1. 김치 디스카운트 = 역발상 기회! (BUT 정도에 따라 차등)
    if signal.kimchi_premium_pct < -2.0:
        # 한국이 저평가 → 해외 대비 싸다 → 매수 기회
        # -2% → +6%, -5% → +10%
        discount_bonus = min(0.10, abs(signal.kimchi_premium_pct) * 0.02)
        mult *= (1.0 + discount_bonus)
        reasons.append(f"contrarian_discount_{signal.kimchi_premium_pct:.1f}%")
    
    # 2. 선행지표 하락 + Bybit 상승 = 역발상 (Confidence 기반)
    if signal.leading_signal == "DOWN" and signal.leading_change_pct < -2.0:
        # Binance 하락 중인데 Bybit이 버티면 역발상
        if signal.leading_confidence > 0.6:
            confidence_bonus = (signal.leading_confidence - 0.5) * 0.16  # 최대 +8%
            mult *= (1.0 + confidence_bonus)
            reasons.append(f"contrarian_divergence_conf{signal.leading_confidence:.2f}")
    
    # 3. 차익거래 역이용
    if signal.arbitrage_direction == "BITHUMB→BYBIT":
        # Bithumb이 더 비싸면 Bybit 저평가
        mult *= 1.04  # +4% 보너스 (완화)
        reasons.append("arbitrage_undervalued")
    
    return mult, reasons


def _adjust_sniper(signal) -> tuple[float, list[str]]:
    """SNIPER: 저격 매수 전략"""
    SCALE = 2.0  # 최적 배율 (공격적, 분리도 +16.5%p, 강력한 필터)
    mult = 1.0
    reasons = []
    
    # 1. 차익 안정성 중요 (급변동 위험)
    if abs(signal.arbitrage_pct) > 1.0:
        mult *= (1.0 - 0.08 * SCALE)  # -8% 페널티 (완화)
        reasons.append("volatile_spread")
    
    # 2. 유동성 매우 중요 (빠른 진입/탈출)
    if signal.liquidity_score > 0.7:
        mult *= (1.0 + 0.06 * SCALE)  # +6% 보너스 (완화)
        reasons.append("sniper_liquidity")
    elif signal.liquidity_score < 0.4:
        mult *= (1.0 - 0.15 * SCALE)  # -15% 페널티 (완화)
        reasons.append("sniper_low_liquidity")
    
    # 3. 선행지표 참고 (Confidence 기반)
    if signal.leading_signal == "UP" and signal.leading_confidence > 0.6:
        confidence_bonus = (signal.leading_confidence - 0.5) * 0.10 * SCALE  # 최대 +5%
        mult *= (1.0 + confidence_bonus)
        reasons.append(f"sniper_uptrend_conf{signal.leading_confidence:.2f}")
    
    return mult, reasons
