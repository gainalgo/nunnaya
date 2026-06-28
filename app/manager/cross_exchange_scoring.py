"""
Cross Exchange Scoring Module
Apply cross-exchange signals to per-strategy scoring
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
    Adjust the score by applying cross-exchange signals

    Args:
        base_score: base score (0.0 ~ 1.0)
        strategy: strategy name ("PINGPONG", "LADDER", "LIGHTNING", etc.)
        coin: coin symbol ("BTC", "ETH")
        cross_signal: CrossExchangeSignal object (None means no signal)

    Returns:
        {
            "adjusted_score": float,    # adjusted score
            "multiplier": float,         # applied multiplier
            "reasons": List[str],        # adjustment reasons
            "liquidity_ok": bool,        # whether liquidity is sufficient
            "arbitrage_risk": float,     # arbitrage risk (0~1)
        }
    """

    if cross_signal is None:
        # No signal: return defaults
        return {
            "adjusted_score": base_score,
            "multiplier": 1.0,
            "reasons": [],
            "liquidity_ok": True,
            "arbitrage_risk": 0.0,
        }
    
    multiplier = 1.0
    reasons = []
    
    # Data age check
    if cross_signal.data_age_sec > 30.0:
        # Data older than 30s loses reliability
        age_penalty = 0.95
        multiplier *= age_penalty
        reasons.append(f"stale_data_{cross_signal.data_age_sec:.0f}s")

    # === Per-strategy adjustment ===

    if strategy == "PINGPONG":
        # Scalping strategy: liquidity + spread (arbitrage) are critical
        mult, reason = _adjust_pingpong(cross_signal)
        multiplier *= mult
        reasons.extend(reason)

    elif strategy == "AUTOLOOP":
        # Auto-adjusting strategy: liquidity + stability matter
        mult, reason = _adjust_autoloop(cross_signal)
        multiplier *= mult
        reasons.extend(reason)

    elif strategy == "LADDER":
        # Averaging-down strategy: arbitrage chance on dips, liquidity essential
        mult, reason = _adjust_ladder(cross_signal)
        multiplier *= mult
        reasons.extend(reason)

    elif strategy == "LIGHTNING":
        # Surge strategy: leading indicator + liquidity are critical
        mult, reason = _adjust_lightning(cross_signal)
        multiplier *= mult
        reasons.extend(reason)

    elif strategy == "GAZUA":
        # Long-term hold: kimchi premium + liquidity matter
        mult, reason = _adjust_gazua(cross_signal)
        multiplier *= mult
        reasons.extend(reason)

    elif strategy == "CONTRARIAN":
        # Contrarian: reverse-exploit arbitrage, kimchi discount opportunity
        mult, reason = _adjust_contrarian(cross_signal)
        multiplier *= mult
        reasons.extend(reason)

    elif strategy == "SNIPER":
        # Sniper buy: liquidity + arbitrage stability
        mult, reason = _adjust_sniper(cross_signal)
        multiplier *= mult
        reasons.extend(reason)

    # Liquidity check (common to all strategies)
    liquidity_ok = cross_signal.liquidity_score >= 0.3
    if cross_signal.liquidity_score < 0.3:
        multiplier *= 0.70  # 30% penalty on low liquidity (eased)
        reasons.append(f"low_liquidity_{cross_signal.liquidity_score:.2f}")

    # Arbitrage risk calculation
    arbitrage_risk = abs(cross_signal.arbitrage_pct) / 5.0  # 5% basis
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


# === Per-strategy adjustment functions ===

def _adjust_pingpong(signal) -> tuple[float, list[str]]:
    """PINGPONG: scalping strategy"""
    mult = 1.0
    reasons = []

    # 1. A wide arbitrage spread is risky (fills may not happen)
    if abs(signal.arbitrage_pct) > 0.5:
        mult *= 0.92  # -8% penalty (eased)
        reasons.append(f"wide_spread_{signal.arbitrage_pct:.2f}%")

    # 2. Bonus when liquidity is good
    if signal.liquidity_score > 0.7:
        mult *= 1.05  # +5% bonus (eased)
        reasons.append("high_liquidity")

    # 3. Leading indicator irrelevant (scalping reacts instantly)

    return mult, reasons


def _adjust_autoloop(signal) -> tuple[float, list[str]]:
    """AUTOLOOP: auto-adjusting strategy"""
    SCALE = 0.75  # Optimal scale (conservative; effect is minor, safety first)
    mult = 1.0
    reasons = []

    # 1. Prefer a stable market (small arbitrage)
    if abs(signal.arbitrage_pct) < 0.3:
        mult *= (1.0 + 0.03 * SCALE)  # +3% bonus (eased)
        reasons.append("stable_market")

    # 2. Liquidity matters
    if signal.liquidity_score > 0.6:
        mult *= (1.0 + 0.05 * SCALE)  # +5% bonus (eased)
        reasons.append("good_liquidity")

    return mult, reasons


def _adjust_ladder(signal) -> tuple[float, list[str]]:
    """LADDER: averaging-down strategy"""
    mult = 1.0
    reasons = []

    # 1. Downtrend + arbitrage chance = averaging-down opportunity
    if signal.leading_signal == "DOWN" and signal.leading_confidence > 0.6:
        # Binance is falling -> Bybit likely to follow -> prepare to average down
        mult *= 0.97  # -3% penalty (eased; crash risk judged by AI)
        reasons.append("downtrend_prepare")

    # 2. Kimchi discount (undervalued) = buying opportunity
    if signal.kimchi_premium_pct < -1.0:  # -1% or lower
        mult *= 1.06  # +6% bonus (eased)
        reasons.append(f"kimchi_discount_{signal.kimchi_premium_pct:.1f}%")

    # 3. Liquidity is critical (averaging down means large buys)
    if signal.liquidity_score < 0.4:
        mult *= 0.80  # -20% penalty (eased, but still important)
        reasons.append("low_liquidity_risk")

    return mult, reasons


def _adjust_lightning(signal) -> tuple[float, list[str]]:
    """LIGHTNING: surge strategy"""
    mult = 1.0
    reasons = []

    # 1. Leading indicator matters (BUT weighted by confidence)
    if signal.leading_signal == "UP" and signal.leading_confidence > 0.6:
        # Binance is surging -> Bybit will follow
        # Confidence-based: 0.6->+3%, 0.7->+6%, 0.8->+9%, 0.9->+12%
        confidence_bonus = (signal.leading_confidence - 0.5) * 0.24  # max 0.12
        mult *= (1.0 + confidence_bonus)
        reasons.append(f"leading_up_{signal.leading_change_pct:.1f}%_conf{signal.leading_confidence:.2f}")
    elif signal.leading_signal == "DOWN":
        # Surge strategy is unsuitable during a downtrend
        mult *= 0.90  # -10% penalty (eased)
        reasons.append("leading_down_risk")

    # 2. Overheated kimchi premium = risk signal
    if signal.kimchi_premium_pct > 3.0:
        mult *= 0.92  # -8% penalty (eased)
        reasons.append(f"overheated_{signal.kimchi_premium_pct:.1f}%")

    # 3. Liquidity essential (surges need fast entry/exit)
    if signal.liquidity_score > 0.7:
        mult *= 1.08  # +8% bonus (eased)
        reasons.append("high_liquidity_burst")

    return mult, reasons


def _adjust_gazua(signal) -> tuple[float, list[str]]:
    """GAZUA: long-term hold strategy"""
    SCALE = 3.0  # Optimal scale (aggressive, separation +5.1%p)
    mult = 1.0
    reasons = []

    # 1. Prefer a normal kimchi premium range
    if -1.0 < signal.kimchi_premium_pct < 2.0:
        mult *= (1.0 + 0.03 * SCALE)  # +3% bonus (eased)
        reasons.append("normal_premium")
    elif signal.kimchi_premium_pct > 5.0:
        # Too overheated brings correction risk
        mult *= (1.0 - 0.06 * SCALE)  # -6% penalty (eased)
        reasons.append("overheated_risk")

    # 2. Leading indicator up = good signal (BUT confidence-based)
    if signal.leading_signal == "UP" and signal.leading_confidence > 0.6:
        confidence_bonus = (signal.leading_confidence - 0.5) * 0.12 * SCALE  # max +6%
        mult *= (1.0 + confidence_bonus)
        reasons.append(f"uptrend_conf{signal.leading_confidence:.2f}")

    # 3. Liquidity (relatively less important for long-term holds)
    if signal.liquidity_score > 0.5:
        mult *= (1.0 + 0.02 * SCALE)  # +2% bonus (eased)
        reasons.append("adequate_liquidity")

    return mult, reasons


def _adjust_contrarian(signal) -> tuple[float, list[str]]:
    """CONTRARIAN: contrarian strategy"""
    mult = 1.0
    reasons = []

    # 1. Kimchi discount = contrarian opportunity! (BUT scaled by degree)
    if signal.kimchi_premium_pct < -2.0:
        # Korea is undervalued -> cheaper than overseas -> buying opportunity
        # -2% -> +6%, -5% -> +10%
        discount_bonus = min(0.10, abs(signal.kimchi_premium_pct) * 0.02)
        mult *= (1.0 + discount_bonus)
        reasons.append(f"contrarian_discount_{signal.kimchi_premium_pct:.1f}%")

    # 2. Leading indicator down + Bybit up = contrarian (confidence-based)
    if signal.leading_signal == "DOWN" and signal.leading_change_pct < -2.0:
        # Binance is falling but Bybit holds up -> contrarian
        if signal.leading_confidence > 0.6:
            confidence_bonus = (signal.leading_confidence - 0.5) * 0.16  # max +8%
            mult *= (1.0 + confidence_bonus)
            reasons.append(f"contrarian_divergence_conf{signal.leading_confidence:.2f}")

    # 3. Reverse-exploit arbitrage
    if signal.arbitrage_direction == "BITHUMB→BYBIT":
        # If Bithumb is pricier, Bybit is undervalued
        mult *= 1.04  # +4% bonus (eased)
        reasons.append("arbitrage_undervalued")

    return mult, reasons


def _adjust_sniper(signal) -> tuple[float, list[str]]:
    """SNIPER: sniper buy strategy"""
    SCALE = 2.0  # Optimal scale (aggressive, separation +16.5%p, strong filter)
    mult = 1.0
    reasons = []

    # 1. Arbitrage stability matters (sharp-move risk)
    if abs(signal.arbitrage_pct) > 1.0:
        mult *= (1.0 - 0.08 * SCALE)  # -8% penalty (eased)
        reasons.append("volatile_spread")

    # 2. Liquidity is critical (fast entry/exit)
    if signal.liquidity_score > 0.7:
        mult *= (1.0 + 0.06 * SCALE)  # +6% bonus (eased)
        reasons.append("sniper_liquidity")
    elif signal.liquidity_score < 0.4:
        mult *= (1.0 - 0.15 * SCALE)  # -15% penalty (eased)
        reasons.append("sniper_low_liquidity")

    # 3. Leading indicator reference (confidence-based)
    if signal.leading_signal == "UP" and signal.leading_confidence > 0.6:
        confidence_bonus = (signal.leading_confidence - 0.5) * 0.10 * SCALE  # max +5%
        mult *= (1.0 + confidence_bonus)
        reasons.append(f"sniper_uptrend_conf{signal.leading_confidence:.2f}")

    return mult, reasons
