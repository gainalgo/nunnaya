"""
Contrarian Scanner Module
- Detect coins moving against a falling market
- Scoring based on Relative Strength and Correlation
- Multiple benchmark support (BTC, ETH, MARKET_AVG, FEAR_GREED)

[CREATED 2026-01-26]
[UPDATED 2026-01-26] Multi-benchmark support
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any, Tuple
from enum import Enum
import time
import math
import os
import logging

from app.core.constants import env_bool, env_float, env_int
from app.core.hyper_price_store import price_store, DEFAULT_EXCHANGE

logger = logging.getLogger(__name__)

# Notification cooldown (prevent re-notifying the same coin)
_notified_markets: Dict[str, float] = {}
_NOTIFY_COOLDOWN_SEC = 1800  # 30 minutes

class BenchmarkType(str, Enum):
    """Benchmark type"""
    BTC = "BTC"              # BTCUSDT
    ETH = "ETH"              # ETHUSDT
    MARKET_AVG = "MARKET_AVG"  # Average return across the whole market
    FEAR_GREED = "FEAR_GREED"  # Fear & Greed Index

BENCHMARK_LABELS: Dict[str, str] = {
    "BTC": "Bitcoin (BTCUSDT)",
    "ETH": "Ethereum (ETHUSDT) - altcoin correlation",
    "MARKET_AVG": "Average return across the whole market",
    "FEAR_GREED": "Fear & Greed Index",
}

@dataclass
class ContrarianCandidate:
    """Contrarian coin candidate"""
    market: str
    coin_ret_pct: float
    benchmark_ret_pct: float  # benchmark return (btc_ret_pct -> benchmark_ret_pct)
    rs: Optional[float]  # Relative Strength (coin_ret / benchmark_ret)
    rs_diff: float       # return difference (coin_ret - benchmark_ret)
    corr: Optional[float]  # Pearson correlation
    score: int           # 0~3 score
    rank: int = 0
    # Volume info (v2)
    volume_ratio: Optional[float] = None  # volume multiple vs average
    volume_spike: bool = False  # whether volume spiked (2x or more)
    # AI score (v3)
    ai_score: Optional[float] = None  # AI success-probability prediction
    # Multi-timeframe (v4)
    tf_score: int = 0  # number of timeframes confirming (0-3)
    # Early detection v2 (2026-02-23)
    rs_momentum: float = 0.0         # RS rate of change (positive = turning strong)
    acceleration: float = 0.0        # acceleration (positive when short-window return is higher)
    early_signal: bool = False        # early-detection flag
    early_reasons: List[str] = field(default_factory=list)  # early-detection reasons

@dataclass
class ContrarianScanResult:
    """Scan result"""
    timestamp: float
    market_down: bool
    benchmark_ret_pct: float  # btc_ret_pct -> benchmark_ret_pct
    benchmark_type: str = "BTC"  # benchmark type used
    benchmark_label: str = ""   # benchmark display label
    candidates: List[ContrarianCandidate] = field(default_factory=list)
    scanned_count: int = 0
    fear_greed_value: Optional[int] = None  # value when F&G is used
    error: Optional[str] = None

class ContrarianScanner:
    """Contrarian coin scanner (multi-benchmark support)"""
    
    def __init__(self):
        self.enabled = env_bool("CONTRARIAN_ENABLED", default=True)
        self.cache_sec = env_float("CONTRARIAN_CACHE_SEC", default=15.0)
        self.lookback_ticks = env_int("CONTRARIAN_LOOKBACK_TICKS", default=30)
        
        # Contrarian-detection thresholds
        self.market_down_th = env_float("CONTRARIAN_MARKET_DOWN_TH", default=-0.1)  # BTC at or below -0.1% = market down
        self.coin_up_th = env_float("CONTRARIAN_COIN_UP_TH", default=0.3)
        self.rs_th = env_float("CONTRARIAN_RS_TH", default=1.2)
        self.corr_th = env_float("CONTRARIAN_CORR_TH", default=0.3)
        self.min_samples = env_int("CONTRARIAN_MIN_SAMPLES", default=5)  # 15→5 for faster startup
        self.rs_eps = 0.2  # RS cannot be computed when benchmark return is at or below this
        # [2026-02-12] Sideways relaxation: still pick candidates by relative strength when benchmark moves very little
        self.sideways_mode_enabled = env_bool("CONTRARIAN_SIDEWAYS_MODE", default=True)
        self.sideways_rs_diff_th = env_float("CONTRARIAN_SIDEWAYS_RS_DIFF_TH", default=0.15)

        # Fear & Greed threshold
        self.fg_fear_threshold = env_int("CONTRARIAN_FG_FEAR_TH", default=40)  # at or below 40 = "market down"

        # Notification settings
        self.notify_enabled = env_bool("CONTRARIAN_NOTIFY_ENABLED", default=True)
        self.notify_min_score = env_int("CONTRARIAN_NOTIFY_MIN_SCORE", default=2)  # notify only at score 2+

        # Volume-spike detection settings
        self.volume_spike_th = env_float("CONTRARIAN_VOLUME_SPIKE_TH", default=2.0)  # 2x or more vs average
        self.volume_history_ticks = env_int("CONTRARIAN_VOLUME_HISTORY", default=30)  # number of ticks for the average

        # Multi-timeframe settings
        self.multi_tf_enabled = env_bool("CONTRARIAN_MULTI_TF_ENABLED", default=True)

        # Enable AI score
        self.ai_score_enabled = env_bool("CONTRARIAN_AI_SCORE_ENABLED", default=True)

        # [2026-02-23] Early-detection settings
        self.early_detect_enabled = env_bool("CONTRARIAN_EARLY_DETECT", default=True)
        self.rs_momentum_th = env_float("CONTRARIAN_RS_MOMENTUM_TH", default=0.3)
        self.acceleration_th = env_float("CONTRARIAN_ACCEL_TH", default=0.1)

        # Auto-deploy settings
        self.auto_deploy_enabled = env_bool("CONTRARIAN_AUTO_DEPLOY_ENABLED", default=False)
        self.auto_deploy_min_score = env_int("CONTRARIAN_AUTO_DEPLOY_MIN_SCORE", default=3)  # auto-deploy only at score 3+
        self.auto_deploy_budget = env_float("CONTRARIAN_AUTO_DEPLOY_BUDGET", default=50.0)  # default 50 USDT

        # Entry-signal criteria (kept separate from notify/selector cache for consistency)
        self.signal_benchmark = str(os.getenv("CONTRARIAN_SIGNAL_BENCHMARK", "BTC") or "BTC").strip().upper()
        if self.signal_benchmark not in BENCHMARK_LABELS:
            self.signal_benchmark = "BTC"
        self.entry_allow_sideways = env_bool("CONTRARIAN_ENTRY_ALLOW_SIDEWAYS", default=False)
        
        # Cache
        self._cache: Optional[ContrarianScanResult] = None
        self._last_scan: float = 0.0
        self._markets: List[str] = []
        self._current_benchmark: str = "BTC"
        self._volume_history: Dict[str, List[float]] = {}  # volume history
        self._system_ref: Any = None  # lazy-resolved HyperSystem (FastAPI app.state.system)

    def set_markets(self, markets: List[str]) -> None:
        """Set the markets to scan"""
        self._markets = [m for m in markets if m.endswith("USDT") and m != "BTCUSDT"]

    def _get_runtime_system(self):
        """Lazily resolve the HyperSystem instance from FastAPI app.state.system."""
        if self._system_ref is not None:
            return self._system_ref
        try:
            from app.main import app
            state = getattr(app, "state", None)
            system = getattr(state, "system", None) if state is not None else None
            if system is not None:
                self._system_ref = system
                return system
        except (KeyError, AttributeError, TypeError) as exc:
            logger.warning("[contrarian_scanner] %s: %s", 'contrarian_scanner._get_runtime_system fallback', exc, exc_info=True)
        return None
    
    def _get_benchmark_status(
        self, 
        benchmark_type: str
    ) -> Tuple[bool, float, List[float], Optional[int]]:
        """
        Compute market state based on the benchmark

        Returns:
            (market_down, benchmark_ret_pct, benchmark_returns, fear_greed_value)
        """
        benchmark_type = benchmark_type.upper()

        if benchmark_type == "ETH":
            # ETH-based (highly correlated with altcoins)
            eth_prices = self._get_prices("ETHUSDT")
            if not eth_prices or len(eth_prices) < self.min_samples:
                return False, 0.0, [], None
            
            eth_ret_pct = self._calc_return_pct(eth_prices)
            eth_returns = self._calc_returns(eth_prices)
            market_down = eth_ret_pct <= self.market_down_th
            return market_down, eth_ret_pct, eth_returns, None
        
        elif benchmark_type == "MARKET_AVG":
            # Whole-market average (excluding BTC, ETH)
            all_returns_pct: List[float] = []
            avg_return_series: List[List[float]] = []
            
            for market in self._markets:
                if market in ("BTCUSDT", "ETHUSDT"):
                    continue
                prices = self._get_prices(market)
                if prices and len(prices) >= self.min_samples:
                    ret_pct = self._calc_return_pct(prices)
                    all_returns_pct.append(ret_pct)
                    avg_return_series.append(self._calc_returns(prices))
            
            if not all_returns_pct:
                # Fallback to BTC
                return self._get_benchmark_status("BTC")
            
            avg_ret_pct = sum(all_returns_pct) / len(all_returns_pct)
            market_down = avg_ret_pct <= self.market_down_th
            
            # Compute average returns series (trimmed to equal length)
            if avg_return_series:
                min_len = min(len(r) for r in avg_return_series)
                if min_len >= 10:
                    avg_returns = []
                    for i in range(min_len):
                        tick_avg = sum(r[i] for r in avg_return_series) / len(avg_return_series)
                        avg_returns.append(tick_avg)
                else:
                    avg_returns = []
            else:
                avg_returns = []
            
            return market_down, avg_ret_pct, avg_returns, None
        
        elif benchmark_type == "FEAR_GREED":
            # Fear & Greed Index based
            try:
                from app.core.fear_greed import get_fear_greed
                fg = get_fear_greed()
                result = fg.fetch()
                fg_value = result.value

                # Convert F&G value to a return: centered at 50, 0-50: negative, 50-100: positive
                # e.g. F&G=25 -> -25%, F&G=75 -> +25%
                benchmark_ret_pct = (fg_value - 50) / 2.0  # scale adjustment (-25% ~ +25%)
                market_down = fg_value <= self.fg_fear_threshold

                # Use BTC for correlation returns (F&G is not a time series)
                btc_prices = self._get_prices("BTCUSDT")
                btc_returns = self._calc_returns(btc_prices) if btc_prices else []
                
                return market_down, benchmark_ret_pct, btc_returns, fg_value
            except (ImportError, AttributeError, TypeError) as e:
                logger.warning("Fear&Greed fetch failed: %s, fallback to BTC", e)
                return self._get_benchmark_status("BTC")
        
        else:  # BTC (default)
            btc_prices = self._get_prices("BTCUSDT")
            if not btc_prices or len(btc_prices) < self.min_samples:
                return False, 0.0, [], None
            
            btc_ret_pct = self._calc_return_pct(btc_prices)
            btc_returns = self._calc_returns(btc_prices)
            market_down = btc_ret_pct <= self.market_down_th
            return market_down, btc_ret_pct, btc_returns, None
    
    def scan(
        self, 
        markets: Optional[List[str]] = None, 
        force: bool = False,
        benchmark_type: str = "BTC",
        notify: bool = True,
    ) -> ContrarianScanResult:
        """Scan all markets to find contrarian coins"""
        now = time.time()
        benchmark_type = benchmark_type.upper()
        self._current_benchmark = benchmark_type

        # Check cache (only for the same benchmark)
        if (not force and self._cache 
            and (now - self._last_scan) < self.cache_sec
            and self._cache.benchmark_type == benchmark_type):
            return self._cache
        
        if markets:
            self.set_markets(markets)
        
        if not self._markets:
            return ContrarianScanResult(
                timestamp=now,
                market_down=False,
                benchmark_ret_pct=0.0,
                benchmark_type=benchmark_type,
                benchmark_label=BENCHMARK_LABELS.get(benchmark_type, benchmark_type),
                error="No markets to scan"
            )
        
        # Compute benchmark state
        market_down, benchmark_ret_pct, benchmark_returns, fg_value = self._get_benchmark_status(benchmark_type)

        if not benchmark_returns and benchmark_type not in ("FEAR_GREED",):
            # Debug: check price_store state
            btc_prices = self._get_prices("BTCUSDT")
            eth_prices = self._get_prices("ETHUSDT")
            debug_info = f"BTC:{len(btc_prices) if btc_prices else 0}, ETH:{len(eth_prices) if eth_prices else 0}, need:{self.min_samples}"
            return ContrarianScanResult(
                timestamp=now,
                market_down=False,
                benchmark_ret_pct=0.0,
                benchmark_type=benchmark_type,
                benchmark_label=BENCHMARK_LABELS.get(benchmark_type, benchmark_type),
                error=f"Insufficient data ({debug_info})"
            )
        
        candidates: List[ContrarianCandidate] = []
        
        for market in self._markets:
            # Exclude ETH when using the ETH benchmark
            if benchmark_type == "ETH" and market == "ETHUSDT":
                continue

            coin_prices = self._get_prices(market)
            if not coin_prices or len(coin_prices) < self.min_samples:
                continue

            coin_ret_pct = self._calc_return_pct(coin_prices)
            coin_returns = self._calc_returns(coin_prices)

            # Compute RS
            rs: Optional[float] = None
            rs_diff = coin_ret_pct - benchmark_ret_pct

            if abs(benchmark_ret_pct) >= self.rs_eps:
                rs = coin_ret_pct / benchmark_ret_pct if benchmark_ret_pct != 0 else None

            # Compute correlation (using benchmark_returns)
            corr = self._calc_correlation(coin_returns, benchmark_returns) if benchmark_returns else None

            # Compute score
            score = 0

            # Condition 1: RS > threshold (or large difference)
            if rs is not None and rs > self.rs_th:
                score += 1
            elif rs is None:
                rs_diff_th = 1.0
                if self.sideways_mode_enabled and not market_down:
                    rs_diff_th = max(0.0, float(self.sideways_rs_diff_th))
                if rs_diff >= rs_diff_th:  # fall back to difference when RS cannot be computed
                    score += 1

            # Condition 2: low correlation
            if corr is not None and corr < self.corr_th:
                score += 1

            # Condition 3: market down + coin up
            if market_down and coin_ret_pct >= self.coin_up_th:
                score += 1

            # Volume-spike detection
            volume_ratio, volume_spike = self._get_volume_ratio(market)

            # Multi-timeframe score
            tf_score = self._get_multi_tf_score(market)

            # AI score
            ai_score = self._get_ai_score(market)

            # Add to candidates if score is at least 1
            if score >= 1:
                candidates.append(ContrarianCandidate(
                    market=market,
                    coin_ret_pct=coin_ret_pct,
                    benchmark_ret_pct=benchmark_ret_pct,
                    rs=rs,
                    rs_diff=rs_diff,
                    corr=corr,
                    score=score,
                    volume_ratio=volume_ratio,
                    volume_spike=volume_spike,
                    ai_score=ai_score,
                    tf_score=tf_score
                ))
        
        # [2026-02-23] Early detection: RS momentum + acceleration
        if self.early_detect_enabled:
            # Fill early-detection fields on existing candidates
            for c in candidates:
                c.rs_momentum = self._calc_rs_momentum(c.market)
                c.acceleration = self._calc_acceleration(c.market)

                early_reasons: List[str] = []
                if c.rs_momentum >= self.rs_momentum_th:
                    early_reasons.append(f"RS momentum:{c.rs_momentum:+.2f}")
                if c.acceleration >= self.acceleration_th:
                    early_reasons.append(f"acceleration:{c.acceleration:+.2f}")
                if c.volume_spike:
                    early_reasons.append("volume spike")

                if c.rs_momentum >= self.rs_momentum_th and (c.acceleration >= self.acceleration_th or c.volume_spike):
                    c.early_signal = True
                    c.early_reasons = early_reasons

            # Add early-detection-only candidates (score 0 but strong early signal)
            existing_markets = {c.market for c in candidates}
            for market in self._markets:
                if market in existing_markets:
                    continue
                if benchmark_type == "ETH" and market == "ETHUSDT":
                    continue

                coin_prices = self._get_prices(market)
                if not coin_prices or len(coin_prices) < self.min_samples:
                    continue

                rs_mom = self._calc_rs_momentum(market)
                accel = self._calc_acceleration(market)
                vol_ratio, vol_spike = self._get_volume_ratio(market)

                if rs_mom >= self.rs_momentum_th and (accel >= self.acceleration_th or vol_spike):
                    coin_ret_pct = self._calc_return_pct(coin_prices)
                    coin_returns = self._calc_returns(coin_prices)
                    rs_diff = coin_ret_pct - benchmark_ret_pct
                    rs = coin_ret_pct / benchmark_ret_pct if abs(benchmark_ret_pct) >= self.rs_eps and benchmark_ret_pct != 0 else None
                    corr = self._calc_correlation(coin_returns, benchmark_returns) if benchmark_returns else None

                    er: List[str] = []
                    if rs_mom >= self.rs_momentum_th:
                        er.append(f"RS momentum:{rs_mom:+.2f}")
                    if accel >= self.acceleration_th:
                        er.append(f"acceleration:{accel:+.2f}")
                    if vol_spike:
                        er.append("volume spike")

                    candidates.append(ContrarianCandidate(
                        market=market,
                        coin_ret_pct=coin_ret_pct,
                        benchmark_ret_pct=benchmark_ret_pct,
                        rs=rs,
                        rs_diff=rs_diff,
                        corr=corr,
                        score=1,
                        volume_ratio=vol_ratio,
                        volume_spike=vol_spike,
                        ai_score=self._get_ai_score(market),
                        tf_score=0,
                        rs_momentum=rs_mom,
                        acceleration=accel,
                        early_signal=True,
                        early_reasons=er,
                    ))

            # Guarantee score of at least 1 for candidates with an early signal
            for c in candidates:
                if c.early_signal and c.score < 1:
                    c.score = 1

        # Sort by score (highest first)
        candidates.sort(key=lambda c: (-c.score, -c.rs_diff, c.corr or 1.0))

        # Assign ranks
        for i, c in enumerate(candidates):
            c.rank = i + 1
        
        result = ContrarianScanResult(
            timestamp=now,
            market_down=market_down,
            benchmark_ret_pct=benchmark_ret_pct,
            benchmark_type=benchmark_type,
            benchmark_label=BENCHMARK_LABELS.get(benchmark_type, benchmark_type),
            candidates=candidates,
            scanned_count=len(self._markets),
            fear_greed_value=fg_value
        )
        
        self._cache = result
        self._last_scan = now
        
        # Send notification (when high-scoring contrarian coins are detected + early signals)
        if notify and (market_down or any(c.early_signal for c in candidates)) and self.notify_enabled:
            self._notify_candidates(candidates, benchmark_type, benchmark_ret_pct)
        
        return result
    
    def _notify_candidates(
        self, 
        candidates: List[ContrarianCandidate], 
        benchmark_type: str,
        benchmark_ret_pct: float
    ) -> None:
        """Telegram notification for high-scoring contrarian coins"""
        global _notified_markets

        now = time.time()
        to_notify: List[ContrarianCandidate] = []
        system = self._get_runtime_system()

        for c in candidates:
            if c.score < self.notify_min_score:
                continue

            # Exclude markets already running as ACTIVE (consistency with deployable candidates)
            if system is not None:
                try:
                    if self._is_already_deployed(system, c.market):
                        continue
                except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
                    logger.warning("[contrarian_scanner] %s: %s", 'excluding markets already running as ACTIVE (consistency with deployable candidates)', exc, exc_info=True)

            # Check cooldown
            last_notified = _notified_markets.get(c.market, 0)
            if (now - last_notified) < _NOTIFY_COOLDOWN_SEC:
                continue

            to_notify.append(c)
            _notified_markets[c.market] = now

        if not to_notify:
            return

        try:
            from app.notify.telegram import send_telegram

            # Build message
            lines = [
                f"🔄 *CONTRARIAN coin detected*",
                f"📏 Benchmark: {benchmark_type} ({benchmark_ret_pct:+.1f}%)",
                f"⚠️ Contrarian coin found during market downturn!",
                ""
            ]

            for c in to_notify[:5]:  # up to 5
                rs_str = f"{c.rs:.2f}" if c.rs else "-"
                corr_str = f"{c.corr:.2f}" if c.corr else "-"
                vol_str = f"📊{c.volume_ratio:.1f}x" if c.volume_ratio else ""
                spike_str = "🔥" if c.volume_spike else ""
                tf_str = f"⏰{c.tf_score}/3" if c.tf_score else ""
                ai_str = f"🤖{c.ai_score:.0%}" if c.ai_score else ""
                early_str = "🔮early" if c.early_signal else ""

                extra_info = " ".join(filter(None, [early_str, spike_str, vol_str, tf_str, ai_str]))

                lines.append(
                    f"• *{c.market}* ⭐{c.score}/3 {extra_info}\n"
                    f"  Return: {c.coin_ret_pct:+.1f}% | RS: {rs_str} | Corr: {corr_str}"
                )

            if len(to_notify) > 5:
                lines.append(f"\n... and {len(to_notify) - 5} more")
            
            message = "\n".join(lines)
            send_telegram(message)
            logger.info(f"Contrarian notify sent: {len(to_notify)} coins")
            
            # Auto-deploy (when enabled)
            if self.auto_deploy_enabled:
                self._auto_deploy_candidates(to_notify)
            
        except (KeyError, IndexError, AttributeError, TypeError, ValueError) as e:
            logger.warning("Contrarian notify failed: %s", e)
    
    def _auto_deploy_candidates(self, candidates: List[ContrarianCandidate]) -> None:
        """Auto-deploy high-scoring contrarian coins (includes budget check)"""
        if not self.auto_deploy_enabled:
            return

        try:
            from app.notify.telegram import send_telegram

            system = self._get_runtime_system()
            if not system:
                logger.warning("Auto-deploy: system not available")
                return

            # Budget check
            remaining_budget = self._check_remaining_budget(system)
            if remaining_budget < self.auto_deploy_budget:
                send_telegram(
                    f"⚠️ *CONTRARIAN insufficient budget*\n\n"
                    f"💵 Remaining budget: {remaining_budget:,.2f} USDT\n"
                    f"💰 Required budget: {self.auto_deploy_budget:,.2f} USDT\n"
                    f"📊 Candidates: {len(candidates)}\n\n"
                    f"_Manual deploy needed after securing budget_"
                )
                logger.info(f"Auto-deploy skipped: budget exhausted (remaining={remaining_budget:.0f})")
                return
            
            deployed = []
            skipped_budget = []
            skipped_existing = []
            
            for c in candidates:
                # Auto-deploy condition: score >= auto_deploy_min_score + volume spike or multi-TF 2+
                if c.score < self.auto_deploy_min_score:
                    continue
                if not (c.volume_spike or c.tf_score >= 2):
                    continue

                # Check if already deployed
                if self._is_already_deployed(system, c.market):
                    skipped_existing.append(c.market)
                    continue

                # Re-check remaining budget
                remaining_budget = self._check_remaining_budget(system)
                if remaining_budget < self.auto_deploy_budget:
                    skipped_budget.append(c.market)
                    continue

                try:
                    # Deploy via the CONTRARIAN strategy (API call)
                    import requests
                    result = requests.post(
                        "http://127.0.0.1:8000/api/ladder/longhold/deploy",
                        json={
                            "market": c.market,
                            "budget_usdt": self.auto_deploy_budget,
                            "strategy": "CONTRARIAN",
                            "params": {
                                "tp": 5.0,
                                "sl": -3.0,
                                "min_score": 2,
                                "auto_deployed": True,
                                "source_score": c.score,
                                "source_volume_spike": c.volume_spike,
                                "source_tf_score": c.tf_score
                            }
                        },
                        timeout=10
                    ).json()
                    
                    if result.get("ok"):
                        deployed.append(c.market)
                        logger.info(f"Auto-deployed CONTRARIAN: {c.market}")
                    elif result.get("error") == "BUDGET_EXHAUSTED":
                        skipped_budget.append(c.market)
                    elif result.get("error") == "ALREADY_DEPLOYED":
                        skipped_existing.append(c.market)
                except Exception as e:
                    logger.warning(f"Auto-deploy failed for {c.market}: {e}")
            
            # Result notification
            if deployed:
                send_telegram(
                    f"🚀 *CONTRARIAN auto-deploy*\n"
                    f"Deployed: {', '.join(deployed)}\n"
                    f"Budget: {self.auto_deploy_budget:,.2f} USDT"
                )
            
            if skipped_budget:
                logger.info("Auto-deploy skipped (budget): %s", skipped_budget)
            if skipped_existing:
                logger.info("Auto-deploy skipped (existing): %s", skipped_existing)
                
        except Exception as e:
            logger.warning("Auto-deploy batch failed: %s", e)
    
    def _check_remaining_budget(self, system) -> float:
        """Compute remaining deployable budget"""
        try:
            equity = float(getattr(system, "_last_equity_usdt", 0) or getattr(system, "equity_usdt", 0) or 0)
            deploy_ratio = float(getattr(system, "deploy_ratio", 0.8) or 0.8)
            total_deployable = equity * deploy_ratio
            
            deployed_usdt = 0.0
            oma = getattr(system, "oma_registry", None)
            if oma and hasattr(oma, "snapshot"):
                snap = oma.snapshot()
                for item in snap.get("active", []):
                    if isinstance(item, dict):
                        deployed_usdt += float(item.get("budget_usdt", 0) or 0)
            
            return max(0.0, total_deployable - deployed_usdt)
        except (KeyError, AttributeError, TypeError, ValueError):
            logger.warning("[Contrarian] _check_remaining_budget error", exc_info=True)
            return 0.0
    
    def _is_already_deployed(self, system, market: str) -> bool:
        """Check whether it is already deployed"""
        try:
            oma = getattr(system, "oma_registry", None)
            if oma and hasattr(oma, "snapshot"):
                snap = oma.snapshot()
                active_markets = {
                    str(x.get("market") if isinstance(x, dict) else x).upper()
                    for x in snap.get("active", [])
                }
                return market.upper() in active_markets
        except (KeyError, AttributeError, TypeError, ValueError) as exc:
            logger.warning("[contrarian_scanner] %s: %s", 'contrarian_scanner._is_already_deployed fallback', exc, exc_info=True)
        return False
    
    def get_candidate(self, market: str) -> Optional[ContrarianCandidate]:
        """Look up the contrarian state for a specific market"""
        if not self._cache:
            return None
        for c in self._cache.candidates:
            if c.market == market:
                return c
        return None
    
    def _refresh_signal_cache(self, market: str, benchmark_type: str) -> None:
        """Quietly refresh the entry-decision cache (no notifications)."""
        benchmark = str(benchmark_type or "BTC").upper()
        now = time.time()
        need_refresh = False
        if self._cache is None:
            need_refresh = True
        elif (now - self._last_scan) >= float(self.cache_sec):
            need_refresh = True
        elif str(getattr(self._cache, "benchmark_type", "BTC")).upper() != benchmark:
            need_refresh = True
        elif market not in self._markets:
            need_refresh = True

        if not need_refresh:
            return

        scan_markets = [m for m in self._markets if isinstance(m, str) and m]
        if market not in scan_markets:
            scan_markets.append(market)
        if not scan_markets:
            scan_markets = [market]

        self.scan(markets=scan_markets, force=True, benchmark_type=benchmark, notify=False)

    def is_contrarian_signal(
        self,
        market: str,
        min_score: int = 2,
        benchmark_type: Optional[str] = None,
        allow_sideways: Optional[bool] = None,
    ) -> Tuple[bool, Optional[ContrarianCandidate]]:
        """Check whether this is a buy signal"""
        market = str(market or "").strip().upper()
        if not market:
            return False, None

        benchmark = str(benchmark_type or self.signal_benchmark or "BTC").strip().upper()
        if benchmark not in BENCHMARK_LABELS:
            benchmark = "BTC"

        try:
            self._refresh_signal_cache(market, benchmark)
        except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as e:
            logger.warning("Contrarian signal cache refresh failed (%s): %s", market, e)

        if not self._cache:
            return False, None

        if allow_sideways is None:
            allow_sideways = bool(self.entry_allow_sideways)

        candidate = self.get_candidate(market)
        if candidate is None:
            return False, None

        # Existing path: market_down + score satisfied
        if (bool(self._cache.market_down) or bool(allow_sideways)) and candidate.score >= max(1, int(min_score)):
            return True, candidate

        # [2026-02-23] Early-detection path: market_down not required
        if self.early_detect_enabled and candidate.early_signal:
            return True, candidate

        return False, None
    
    def _get_prices(self, market: str) -> List[float]:
        """Look up price history"""
        if hasattr(price_store, 'get_prices'):
            prices = price_store.get_prices(market, count=self.lookback_ticks, exchange=DEFAULT_EXCHANGE)
            if prices:
                return list(prices)
        
        # fallback
        current = price_store.get_price(market, exchange=DEFAULT_EXCHANGE)
        if current:
            return [current]
        return []
    
    def _calc_return_pct(self, prices: List[float]) -> float:
        """Compute return (%)"""
        if len(prices) < 2:
            return 0.0
        first, last = prices[0], prices[-1]
        if first <= 0:
            return 0.0
        return (last - first) / first * 100.0
    
    def _calc_returns(self, prices: List[float]) -> List[float]:
        """Compute per-tick return time series"""
        if len(prices) < 2:
            return []
        returns = []
        for i in range(1, len(prices)):
            if prices[i-1] > 0:
                ret = (prices[i] - prices[i-1]) / prices[i-1]
                returns.append(ret)
        return returns
    
    def _calc_correlation(self, x: List[float], y: List[float]) -> Optional[float]:
        """Compute Pearson correlation coefficient (without numpy)"""
        n = min(len(x), len(y))
        if n < 10:
            return None
        
        x, y = x[:n], y[:n]
        
        mean_x = sum(x) / n
        mean_y = sum(y) / n
        
        var_x = sum((xi - mean_x) ** 2 for xi in x)
        var_y = sum((yi - mean_y) ** 2 for yi in y)
        
        if var_x == 0 or var_y == 0:
            return None
        
        cov_xy = sum((x[i] - mean_x) * (y[i] - mean_y) for i in range(n))
        
        corr = cov_xy / math.sqrt(var_x * var_y)
        return max(-1.0, min(1.0, corr))
    
    def _get_volume_ratio(self, market: str) -> Tuple[Optional[float], bool]:
        """Detect volume spikes

        Returns:
            (volume_ratio, is_spike) - multiple vs average, whether it spiked
        """
        try:
            current_vol = price_store.get_volume(market, exchange=DEFAULT_EXCHANGE)
            if not current_vol or current_vol <= 0:
                return None, False

            # Compute average from history
            history = self._volume_history.get(market, [])
            if len(history) < 5:
                # When history is insufficient, only store the current value
                if market not in self._volume_history:
                    self._volume_history[market] = []
                self._volume_history[market].append(current_vol)
                if len(self._volume_history[market]) > self.volume_history_ticks:
                    self._volume_history[market] = self._volume_history[market][-self.volume_history_ticks:]
                return None, False
            
            avg_vol = sum(history) / len(history)
            if avg_vol <= 0:
                return None, False
            
            ratio = current_vol / avg_vol
            is_spike = ratio >= self.volume_spike_th

            # Update history
            self._volume_history[market].append(current_vol)
            if len(self._volume_history[market]) > self.volume_history_ticks:
                self._volume_history[market] = self._volume_history[market][-self.volume_history_ticks:]
            
            return round(ratio, 2), is_spike
        except (KeyError, AttributeError, TypeError, ValueError):
            logger.warning("[Contrarian] _get_volume_info error for %s", market, exc_info=True)
            return None, False
    
    def _get_ai_score(self, market: str) -> Optional[float]:
        """Predict contrarian-coin success probability with the AI model"""
        if not self.ai_score_enabled:
            return None
        
        try:
            # v3-H: HyperSystem -> engine(HyperNunnayaEngine) -> pipeline -> brain.analyze()
            system = self._get_runtime_system()
            engine = getattr(system, "engine", None) if system is not None else None
            pipeline = getattr(engine, "pipeline", None) if engine is not None else None
            brain = getattr(pipeline, "brain", None) if pipeline is not None else None
            if brain is None or not hasattr(brain, "analyze"):
                return None

            prices = self._get_prices(market)
            if not prices:
                return None
            price = float(prices[-1])
            if (not math.isfinite(price)) or price <= 0.0:
                return None

            # Use StrategyBrainOutput.ai_prediction (0~1)
            brain_out = brain.analyze(
                market=market,
                price=price,
                price_history=prices[-20:],
                context=None,
            )
            ai_prediction = getattr(brain_out, "ai_prediction", None)
            if ai_prediction is None and hasattr(brain_out, "to_dict"):
                try:
                    ai_prediction = brain_out.to_dict().get("ai_prediction")
                except (KeyError, AttributeError, TypeError):
                    logger.warning("ai_prediction extraction from brain_out.to_dict() failed", exc_info=True)
                    ai_prediction = None
            if ai_prediction is None:
                return None

            score = float(ai_prediction)
            if not math.isfinite(score):
                return None
            return max(0.0, min(1.0, score))
        except (TypeError, ValueError):
            logger.warning("[Contrarian] _get_ai_score type error for %s", market, exc_info=True)
            return None
        except (KeyError, IndexError, AttributeError, TypeError, ValueError):
            logger.warning("[Contrarian] _get_ai_score error for %s", market, exc_info=True)
            return None
    
    def _get_multi_tf_score(self, market: str) -> int:
        """Multi-timeframe contrarian score

        Confirm contrarian signals across multiple timeframes:
        - 5 min (current lookback)
        - 15 min (3x lookback)
        - 30 min (6x lookback)

        Returns:
            0-3 score (+1 for each timeframe showing contrarian behavior)
        """
        if not self.multi_tf_enabled:
            return 0
        
        try:
            btc_prices_5m = price_store.get_prices("BTCUSDT", count=self.lookback_ticks, exchange=DEFAULT_EXCHANGE)
            btc_prices_15m = price_store.get_prices("BTCUSDT", count=self.lookback_ticks * 3, exchange=DEFAULT_EXCHANGE)
            btc_prices_30m = price_store.get_prices("BTCUSDT", count=self.lookback_ticks * 6, exchange=DEFAULT_EXCHANGE)
            
            coin_prices_5m = price_store.get_prices(market, count=self.lookback_ticks, exchange=DEFAULT_EXCHANGE)
            coin_prices_15m = price_store.get_prices(market, count=self.lookback_ticks * 3, exchange=DEFAULT_EXCHANGE)
            coin_prices_30m = price_store.get_prices(market, count=self.lookback_ticks * 6, exchange=DEFAULT_EXCHANGE)
            
            score = 0
            
            # 5 min check
            if btc_prices_5m and coin_prices_5m and len(btc_prices_5m) >= 5 and len(coin_prices_5m) >= 5:
                btc_ret = self._calc_return_pct(list(btc_prices_5m))
                coin_ret = self._calc_return_pct(list(coin_prices_5m))
                if btc_ret < 0 and coin_ret > 0:
                    score += 1

            # 15 min check
            if btc_prices_15m and coin_prices_15m and len(btc_prices_15m) >= 10 and len(coin_prices_15m) >= 10:
                btc_ret = self._calc_return_pct(list(btc_prices_15m))
                coin_ret = self._calc_return_pct(list(coin_prices_15m))
                if btc_ret < 0 and coin_ret > 0:
                    score += 1

            # 30 min check
            if btc_prices_30m and coin_prices_30m and len(btc_prices_30m) >= 15 and len(coin_prices_30m) >= 15:
                btc_ret = self._calc_return_pct(list(btc_prices_30m))
                coin_ret = self._calc_return_pct(list(coin_prices_30m))
                if btc_ret < 0 and coin_ret > 0:
                    score += 1
            
            return score
        except (KeyError, AttributeError, TypeError):
            logger.warning("[Contrarian] _get_multi_tf_score error for %s", market, exc_info=True)
            return 0
    
    def _calc_rs_momentum(self, market: str) -> float:
        """Compute RS rate of change: recent-window RS vs older-window RS.
        Positive = relative strength is increasing (start-of-contrarian signal).
        """
        coin_prices = self._get_prices(market)
        if not coin_prices or len(coin_prices) < self.lookback_ticks * 2:
            return 0.0

        half = len(coin_prices) // 2
        recent_ret = self._calc_return_pct(coin_prices[half:])
        older_ret = self._calc_return_pct(coin_prices[:half])

        btc_prices = self._get_prices("BTCUSDT")
        if not btc_prices or len(btc_prices) < self.lookback_ticks * 2:
            return 0.0

        btc_half = len(btc_prices) // 2
        btc_recent_ret = self._calc_return_pct(btc_prices[btc_half:])
        btc_older_ret = self._calc_return_pct(btc_prices[:btc_half])

        rs_recent = recent_ret - btc_recent_ret
        rs_older = older_ret - btc_older_ret

        return rs_recent - rs_older

    def _calc_acceleration(self, market: str) -> float:
        """Compute acceleration: positive when the short-window return is stronger than the long-window.
        Positive = accelerating upward, negative = decelerating.
        """
        prices = self._get_prices(market)
        if not prices or len(prices) < 15:
            return 0.0

        n = len(prices)
        ret_short = self._calc_return_pct(prices[max(0, n - 5):])
        ret_mid = self._calc_return_pct(prices[max(0, n - 10):])
        ret_long = self._calc_return_pct(prices[max(0, n - 15):])

        ret_short_per_tick = ret_short / 5.0
        ret_mid_per_tick = ret_mid / 10.0
        ret_long_per_tick = ret_long / 15.0

        accel = (ret_short_per_tick - ret_long_per_tick) * 10
        return accel

    def to_dict(self) -> Dict[str, Any]:
        """Return the current state as a dict (for the API)"""
        if not self._cache:
            return {
                "enabled": self.enabled,
                "market_down": False,
                "benchmark_ret_pct": 0.0,
                "benchmark_type": "BTC",
                "benchmark_label": BENCHMARK_LABELS.get("BTC", "Bitcoin"),
                "candidates": [],
                "scanned_count": 0,
                "timestamp": 0,
                "available_benchmarks": list(BENCHMARK_LABELS.items())
            }
        
        return {
            "enabled": self.enabled,
            "market_down": self._cache.market_down,
            "benchmark_ret_pct": round(self._cache.benchmark_ret_pct, 2),
            "benchmark_type": self._cache.benchmark_type,
            "benchmark_label": self._cache.benchmark_label,
            "fear_greed_value": self._cache.fear_greed_value,
            "candidates": [
                {
                    "market": c.market,
                    "coin_ret_pct": round(c.coin_ret_pct, 2),
                    "benchmark_ret_pct": round(c.benchmark_ret_pct, 2),
                    "rs": round(c.rs, 2) if c.rs else None,
                    "rs_diff": round(c.rs_diff, 2),
                    "corr": round(c.corr, 2) if c.corr else None,
                    "score": c.score,
                    "rank": c.rank,
                    # New fields (v2)
                    "volume_ratio": c.volume_ratio,
                    "volume_spike": c.volume_spike,
                    "ai_score": round(c.ai_score, 2) if c.ai_score else None,
                    "tf_score": c.tf_score,
                    "rs_momentum": round(c.rs_momentum, 3),
                    "acceleration": round(c.acceleration, 3),
                    "early_signal": c.early_signal,
                    "early_reasons": c.early_reasons,
                }
                for c in self._cache.candidates[:20]  # top 20 only
            ],
            "scanned_count": self._cache.scanned_count,
            "timestamp": self._cache.timestamp,
            "available_benchmarks": list(BENCHMARK_LABELS.items()),
            "params": {
                "lookback_ticks": self.lookback_ticks,
                "market_down_th": self.market_down_th,
                "coin_up_th": self.coin_up_th,
                "rs_th": self.rs_th,
                "corr_th": self.corr_th,
                "min_samples": self.min_samples,
                "fg_fear_threshold": self.fg_fear_threshold
            }
        }

# Singleton
_contrarian_scanner: Optional[ContrarianScanner] = None

def get_contrarian_scanner() -> ContrarianScanner:
    global _contrarian_scanner
    if _contrarian_scanner is None:
        _contrarian_scanner = ContrarianScanner()
    return _contrarian_scanner
