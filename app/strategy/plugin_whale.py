# Extracted from strategy_plugins.py — Phase 2 (file diet)
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from app.strategy import indicators
from app.strategy.strategy_base import Decision, StrategyPlugin

logger = logging.getLogger(__name__)

class NotImplementedPlugin(StrategyPlugin):
    """Safe plugin for strategies not yet ported (always HOLD)."""

    def __init__(self, name: str):
        self.name = name

    def decide(self, ctx: Any, price: float) -> Decision:
        return Decision(signal="hold", reason=f"{self.name}:not_implemented", meta={})

# ======================================================================
# WHALE Plugin
# Strategy core: insight from an acquaintance — "whales live under a big cloud"
#
# Entry (AND conditions):
#   1. Volume spike: recent 3m candle volume > N-period average x vol_spike_ratio
#   2. Two candles above cloud: last 2 3m candle closes both above Ichimoku cloud_top
#   3. StochRSI crossover: %K just crossed above %D (bullish crossover)
#
# Exit:
#   - Two candles below cloud: last 2 closes both below cloud_bottom -> sell immediately
#   - TP/SL safety net
# ======================================================================
class WhalePlugin(StrategyPlugin):
    """Ride-the-whale strategy — Ichimoku + StochRSI + volume spike."""

    name: str = "WHALE"

    # State keys
    _ST_IDLE   = "IDLE"
    _ST_ACTIVE = "ACTIVE"

    def __init__(self) -> None:
        # Per-market 3m candle cache {market: (ts, candles)}
        self._candle_cache: Dict[str, Tuple[float, list]] = {}
        self._lock = threading.Lock()
        # Set of markets currently refreshing in background (prevents duplicate fetch)
        self._fetching: set = set()

    # ------------------------------------------------------------------
    # Candle fetch (30s cache + async background refresh)
    # On cache expiry, return stale data immediately and refresh in background
    # -> no tick blocking
    # ------------------------------------------------------------------
    def _get_candles(self, market: str, unit: int = 3, count: int = 80) -> list:
        now = time.time()
        with self._lock:
            cached = self._candle_cache.get(market)
            cache_age = (now - cached[0]) if cached else 9999.0
            if cached and cache_age < 30.0:
                return cached[1]  # fresh cache
            stale = cached[1] if cached else None

        # Cache expired — background refresh + return stale immediately (avoids tick blocking)
        if market not in self._fetching:
            self._fetching.add(market)
            def _bg_fetch(_m=market, _u=unit, _c=count, _now=now):
                try:
                    from app.core.multi_timeframe_ai import fetch_candles
                    candles = fetch_candles(_m, unit=_u, count=_c)
                    if candles:
                        with self._lock:
                            self._candle_cache[_m] = (time.time(), candles)
                except (OSError, TypeError, ValueError, OverflowError) as e:
                    logger.warning("[WHALE] bg candle fetch failed %s: %s", _m, e, exc_info=True)
                finally:
                    self._fetching.discard(_m)
            threading.Thread(target=_bg_fetch, daemon=True).start()

        # Return stale cache if present, otherwise (first call) fetch synchronously
        if stale is not None:
            return stale
        try:
            from app.core.multi_timeframe_ai import fetch_candles
            candles = fetch_candles(market, unit=unit, count=count)
            if candles:
                with self._lock:
                    self._candle_cache[market] = (time.time(), candles)
            return candles or []
        except (OSError, TypeError, ValueError, OverflowError) as e:
            logger.warning("[WHALE] candle fetch failed %s: %s", market, e, exc_info=True)
            return []

    # ------------------------------------------------------------------
    # State management
    # ------------------------------------------------------------------
    def _get_state(self, ctx: Any) -> str:
        return str(getattr(ctx, "_whale_state", self._ST_IDLE))

    def _set_state(self, ctx: Any, state: str) -> None:
        ctx._whale_state = state

    def _reset(self, ctx: Any) -> None:
        ctx._whale_state = self._ST_IDLE
        ctx._whale_entry_price = 0.0

    # ------------------------------------------------------------------
    # Entry signal analysis
    # ------------------------------------------------------------------
    def _check_entry(self, market: str, candles: list, params: Dict) -> Tuple[bool, str]:
        """Returns True when all four conditions are met.

        1. RSI <= 30 (checked over recent N candles) — whale accumulation zone
        2. Two candles breaking above a thick cloud — whale surfacing signal
        3. Volume spike — confirms buying pressure
        4. StochRSI %K > %D crossover — confirms momentum
        """
        if len(candles) < 60:
            return False, f"candles too few: {len(candles)}"

        closes  = [float(c.get("trade_price") or c.get("close") or 0) for c in candles]
        highs   = [float(c.get("high_price")  or c.get("high")  or closes[i]) for i, c in enumerate(candles)]
        lows    = [float(c.get("low_price")   or c.get("low")   or closes[i]) for i, c in enumerate(candles)]
        volumes = [float(c.get("candle_acc_trade_volume") or c.get("volume") or 0) for c in candles]

        if not closes or closes[-1] <= 0:
            return False, "invalid close"

        # ── 1. Check RSI <= 30 (whale accumulation zone) ──────────────────
        rsi_len = int(params.get("rsi_period", 14))
        rsi_entry_max = float(params.get("rsi_entry_max", 30.0))
        rsi_entry_lookback = int(params.get("rsi_entry_lookback", 5))  # at least once in recent N candles
        rsi_min_recent = 100.0
        for i in range(rsi_entry_lookback):
            end_idx = len(closes) - i
            if end_idx < rsi_len + 1:
                break
            r = indicators.rsi(closes[:end_idx], rsi_len)
            if r is not None:
                rsi_min_recent = min(rsi_min_recent, r)
        if rsi_min_recent > rsi_entry_max:
            return False, f"RSI not met: min_recent={rsi_min_recent:.1f} > {rsi_entry_max:.0f}"

        # ── 2. Two candles breaking above a thick cloud ─────────────────────────
        cloud = indicators.ichimoku_cloud(
            highs, lows, closes,
            tenkan=int(params.get("ichimoku_tenkan", 9)),
            kijun=int(params.get("ichimoku_kijun", 26)),
            senkou_b_period=int(params.get("ichimoku_senkou_b", 52)),
        )
        if cloud is None:
            return False, "ichimoku: insufficient data"

        # Check cloud thickness — thicker means higher confidence
        cloud_mid = (cloud["cloud_top"] + cloud["cloud_bottom"]) / 2.0
        cloud_min_thickness_pct = float(params.get("cloud_min_thickness_pct", 1.5))
        thickness_pct = 0.0
        if cloud_mid > 0:
            thickness_pct = (cloud["cloud_top"] - cloud["cloud_bottom"]) / cloud_mid * 100.0
        if thickness_pct < cloud_min_thickness_pct:
            return False, f"cloud too thin: {thickness_pct:.2f}% < {cloud_min_thickness_pct}%"

        c1_above = closes[-2] > cloud["cloud_top"]
        c2_above = closes[-1] > cloud["cloud_top"]
        if not (c1_above and c2_above):
            return False, (
                f"cloud not broken: c1={closes[-2]:.1f} c2={closes[-1]:.1f} "
                f"top={cloud['cloud_top']:.1f}"
            )

        # ── 3. Volume spike ─────────────────────────────────────
        vol_lookback = int(params.get("vol_lookback", 20))
        vol_spike_ratio = float(params.get("vol_spike_ratio", 2.0))
        if len(volumes) < vol_lookback + 1:
            return False, "insufficient volume history"
        recent_vol = volumes[-1]
        avg_vol    = sum(volumes[-vol_lookback - 1: -1]) / vol_lookback
        if avg_vol <= 0 or recent_vol < avg_vol * vol_spike_ratio:
            return False, f"no volume spike: {recent_vol:.0f} / avg {avg_vol:.0f} (need x{vol_spike_ratio})"

        # ── 4. StochRSI %K > %D crossover ──────────────────────────────
        srsi = indicators.stochastic_rsi(
            closes,
            rsi_period=int(params.get("stoch_rsi_period", 14)),
            stoch_period=14,
            k_smooth=int(params.get("stoch_k_smooth", 3)),
            d_smooth=int(params.get("stoch_d_smooth", 3)),
        )
        if srsi is None:
            return False, "StochRSI: insufficient data"

        if not srsi["crossover"]:
            return False, f"no crossover: k={srsi['k']:.1f} d={srsi['d']:.1f}"

        return True, (
            f"RSI_min={rsi_min_recent:.1f} "
            f"cloud_two={thickness_pct:.1f}% "
            f"vol={recent_vol/avg_vol:.1f}x "
            f"k={srsi['k']:.1f}>d={srsi['d']:.1f}"
        )

    # ------------------------------------------------------------------
    # Exit signal analysis
    # ------------------------------------------------------------------
    def _check_exit(self, candles: list, entry_price: float, price: float, params: Dict) -> Tuple[bool, str]:
        """Exit conditions (in priority order):
        1. SL — stop loss first
        2. RSI ≥ 65 — whale profit-taking zone (starting to exit)
        3. Two candles below cloud — confirms trend reversal
        4. TP safety net
        """
        tp_pct       = float(params.get("tp_pct", 2.0))
        sl_pct       = float(params.get("sl_pct", 3.0))
        rsi_exit_min = float(params.get("rsi_exit_min", 65.0))
        rsi_len      = int(params.get("rsi_period", 14))

        if entry_price > 0:
            pnl = (price - entry_price) / entry_price * 100.0
            if pnl <= -sl_pct:
                return True, f"SL {pnl:+.2f}%"

        if len(candles) < 60:
            return False, ""

        closes = [float(c.get("trade_price") or c.get("close") or 0) for c in candles]
        highs  = [float(c.get("high_price")  or c.get("high")  or closes[i]) for i, c in enumerate(candles)]
        lows   = [float(c.get("low_price")   or c.get("low")   or closes[i]) for i, c in enumerate(candles)]

        # ── RSI ≥ 65: whale profit-taking zone -> starting to exit ─────────
        rsi_val = indicators.rsi(closes, rsi_len)
        if rsi_val is not None and rsi_val >= rsi_exit_min:
            pnl = (price - entry_price) / entry_price * 100.0 if entry_price > 0 else 0.0
            return True, f"RSI exit {rsi_val:.1f}≥{rsi_exit_min:.0f} pnl={pnl:+.2f}%"

        # ── Two candles below cloud: trend reversal ──────────────────────────
        cloud = indicators.ichimoku_cloud(
            highs, lows, closes,
            tenkan=int(params.get("ichimoku_tenkan", 9)),
            kijun=int(params.get("ichimoku_kijun", 26)),
            senkou_b_period=int(params.get("ichimoku_senkou_b", 52)),
        )
        if cloud is not None:
            c1_below = closes[-2] < cloud["cloud_bottom"]
            c2_below = closes[-1] < cloud["cloud_bottom"]
            if c1_below and c2_below:
                return True, (
                    f"2 candles below cloud: c1={closes[-2]:.1f} c2={closes[-1]:.1f} "
                    f"bottom={cloud['cloud_bottom']:.1f}"
                )

        # ── TP safety net ───────────────────────────────────────────
        if entry_price > 0:
            pnl = (price - entry_price) / entry_price * 100.0
            if pnl >= tp_pct:
                return True, f"TP {pnl:+.2f}%"

        return False, ""

    # ------------------------------------------------------------------
    # Main decision logic
    # ------------------------------------------------------------------
    def decide(self, ctx: Any, price: float) -> Decision:
        market = str(getattr(ctx, "market", "") or "")
        params = dict(getattr(ctx, "params", {}) or {})

        # Parameter defaults
        candle_unit = int(params.get("candle_unit", 3))

        state = self._get_state(ctx)
        candles = self._get_candles(market, unit=candle_unit)

        # ── IDLE → entry check ──────────────────────────────────────
        if state == self._ST_IDLE:
            ok, reason = self._check_entry(market, candles, params)
            if ok:
                self._set_state(ctx, self._ST_ACTIVE)
                ctx._whale_entry_price = price
                logger.info("[WHALE] %s entry → %s", market, reason)
                return Decision(
                    signal="buy",
                    reason=f"WHALE_ENTRY: {reason}",
                    meta={"whale_entry_price": price, "reason": reason},
                )
            return Decision(signal="hold", reason=f"WHALE_WAIT: {reason}")

        # ── ACTIVE → exit check ────────────────────────────────────
        if state == self._ST_ACTIVE:
            entry_price = float(getattr(ctx, "_whale_entry_price", 0.0))
            ok, reason = self._check_exit(candles, entry_price, price, params)
            if ok:
                self._reset(ctx)
                logger.info("[WHALE] %s exit → %s", market, reason)
                return Decision(
                    signal="sell",
                    reason=f"WHALE_EXIT: {reason}",
                    meta={"whale_exit_reason": reason},
                )
            pnl = (price - entry_price) / entry_price * 100.0 if entry_price > 0 else 0.0
            return Decision(signal="hold", reason=f"WHALE_HOLD pnl={pnl:+.2f}%")

        # Safe fallback
        self._reset(ctx)
        return Decision(signal="hold", reason="WHALE_RESET")

    # ------------------------------------------------------------------
    # Full market scanner — called when autopilot fills a WHALE slot
    # Scans all USDT markets on 3m candles and returns coins meeting whale conditions
    # ------------------------------------------------------------------
    def scan_markets(
        self,
        market_list: List[str],
        params: Optional[Dict] = None,
        exclude: Optional[set] = None,
    ) -> List[Dict]:
        """Returns the list of markets that meet the whale entry conditions.

        Args:
            market_list: list of markets to scan (e.g. all USDT markets)
            params: strategy parameters (None to use defaults)
            exclude: markets already in use (excluded)

        Returns:
            [{"market": str, "reason": str, "score": float}, ...]
            higher score means stronger signal
        """
        if params is None:
            params = {}
        if exclude is None:
            exclude = set()

        results = []
        for market in market_list:
            if market in exclude:
                continue
            try:
                candles = self._get_candles(market, unit=3, count=80)
                ok, reason = self._check_entry(market, candles, params)
                if ok:
                    # score: lower RSI + thicker cloud => higher score
                    score = 1.0
                    try:
                        closes = [float(c.get("trade_price") or c.get("close") or 0) for c in candles]
                        r = indicators.rsi(closes, int(params.get("rsi_period", 14)))
                        if r is not None:
                            score += max(0.0, (30.0 - r) / 10.0)  # lower RSI => +points
                    except (KeyError, AttributeError, TypeError, ValueError) as exc:
                        logger.warning("[plugin_whale] %s: %s", 'score: lower RSI + thicker cloud => higher score', exc, exc_info=True)
                    results.append({"market": market, "reason": reason, "score": score})
                    logger.info("[WHALE/SCAN] 🐋 signal found! %s — %s", market, reason)
            except (KeyError, AttributeError, TypeError, ValueError) as e:
                logger.warning("[WHALE/SCAN] %s scan failed: %s", market, e, exc_info=True)

        results.sort(key=lambda x: x["score"], reverse=True)
        return results

