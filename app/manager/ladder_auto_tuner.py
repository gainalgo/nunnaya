# ============================================================
# File: app/manager/ladder_auto_tuner.py
# Autocoin OS v3-H — LADDER Auto-Tuner
# ------------------------------------------------------------
# 시장 상황에 따라 LADDER 전략 파라미터를 자동 조정
# - 멀티 타임프레임 분석 (24h / 7d / 30d)
# - 국면 분류 + 히스테리시스
# - 외부 시그널 통합 (F&G, BTC Leading, Volume Spike, Regime)
# - 성과 피드백 루프
# - 수익성 바닥 보장 (수수료+슬리피지+안전마진)
# ============================================================

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Dict, List, Optional

from app.core.constants import env_bool, env_float, env_int

logger = logging.getLogger(__name__)

clamp = lambda x, lo, hi: max(lo, min(hi, x))

TUNE_HISTORY_PATH = os.path.join("runtime", "ladder_tune_history.json")
TUNE_HISTORY_MAX = 500

FEE_BPS = 10
SAFETY_BPS = 10


# ── Enums & Dataclasses ──────────────────────────────────────

class TunerRegime(Enum):
    QUIET = "QUIET"
    NORMAL = "NORMAL"
    ACTIVE = "ACTIVE"
    EXTREME = "EXTREME"
    CRASH = "CRASH"


@dataclass
class MarketAnalysis:
    market: str
    amplitude_24h_pct: float
    amplitude_7d_pct: float
    atr_pct: float
    volume_trend: str
    trend_direction: str
    bounce_rate: float
    current_price: float
    fear_greed_value: int
    btc_signal_direction: str
    btc_signal_strength: float
    volume_spike_ratio: float
    market_regime: str
    timestamp: float


@dataclass
class TuneResult:
    market: str
    regime: str
    step_pct: float
    max_steps: int
    order_usdt: int
    martingale: float
    tp_pct: float
    trailing_stop_pct: float
    spacing_value: float
    circuit_breaker: bool
    reason: str
    adjustments: List[str] = field(default_factory=list)
    timestamp: float = 0.0


# ── Regime Bounds ────────────────────────────────────────────

_REGIME_BOUNDS: Dict[str, Dict[str, tuple]] = {
    "QUIET": {
        "step_pct":         (0.30, 0.70),
        "max_steps":        (15, 30),
        "order_usdt":        (5, 15),
        "martingale":       (1.00, 1.05),
        "tp_pct":           (0.50, 1.20),
        "trailing_stop_pct":(0.25, 0.60),
    },
    "NORMAL": {
        "step_pct":         (0.70, 1.50),
        "max_steps":        (8, 16),
        "order_usdt":        (10, 30),
        "martingale":       (1.00, 1.15),
        "tp_pct":           (1.00, 2.50),
        "trailing_stop_pct":(0.50, 1.00),
    },
    "ACTIVE": {
        "step_pct":         (1.50, 3.00),
        "max_steps":        (5, 10),
        "order_usdt":        (20, 50),
        "martingale":       (1.05, 1.20),
        "tp_pct":           (2.00, 4.00),
        "trailing_stop_pct":(0.80, 1.50),
    },
    "EXTREME": {
        "step_pct":         (2.50, 5.00),
        "max_steps":        (3, 7),
        "order_usdt":        (30, 70),
        "martingale":       (1.10, 1.25),
        "tp_pct":           (3.00, 6.00),
        "trailing_stop_pct":(1.00, 2.50),
    },
}


# ── LadderAutoTuner ──────────────────────────────────────────

class LadderAutoTuner:
    """LADDER 전략 파라미터 자동 조정 엔진"""

    def __init__(
        self,
        ladder_manager: Any,
        candle_loader: Any | None = None,
        regime_detector: Any | None = None,
        fear_greed: Any | None = None,
        btc_signal: Any | None = None,
        volume_spike: Any | None = None,
        system: Any | None = None,
    ):
        self._lm = ladder_manager
        self._system = system

        if candle_loader is None:
            try:
                from app.backtest.candle_loader import CandleLoader
                candle_loader = CandleLoader()
            except (ImportError, AttributeError, TypeError) as exc:
                logger.warning("[LADDER_TUNE] __init__ candle_loader fallback: %s", exc, exc_info=True)
        self._candle_loader = candle_loader

        if system is not None:
            if regime_detector is None:
                regime_detector = getattr(system, "regime_detector", None)
            if fear_greed is None:
                fear_greed = getattr(system, "fear_greed", None)
            if btc_signal is None:
                btc_signal = getattr(system, "btc_leading_signal", None)
            if volume_spike is None:
                volume_spike = getattr(system, "volume_spike_detector", None)

        self._regime_detector = regime_detector
        self._fear_greed = fear_greed
        self._btc_signal = btc_signal
        self._volume_spike = volume_spike

        self.enabled = env_bool("LADDER_TUNE_ENABLED", default=False)
        self.crash_drop_pct = env_float("LADDER_TUNE_CRASH_DROP_PCT", default=7.0)
        self.crash_window_hours = env_int("LADDER_TUNE_CRASH_WINDOW_H", default=3)
        self.crash_cooldown_sec = env_int("LADDER_TUNE_CRASH_COOLDOWN_SEC", default=7200)

        self._prev_regime: Dict[str, TunerRegime] = {}
        self._regime_since: Dict[str, float] = {}
        self._hysteresis_sec = env_float("LADDER_TUNE_HYSTERESIS_SEC", default=300.0)

        # Time-based rotation policy (long-stuck ladder positions)
        # - Soft timeout: warning only
        # - Profit timeout: rotate if pnl >= min profit
        # - Hard timeout: warning (if not profitable), rotate if profitable
        self.rotate_enabled = env_bool("LADDER_ROTATE_ENABLED", default=False)
        self.rotate_soft_timeout_h = env_float("LADDER_ROTATE_SOFT_TIMEOUT_H", default=24.0)
        self.rotate_profit_timeout_h = env_float("LADDER_ROTATE_PROFIT_TIMEOUT_H", default=48.0)
        self.rotate_hard_timeout_h = env_float("LADDER_ROTATE_HARD_TIMEOUT_H", default=72.0)
        self.rotate_min_profit_pct = env_float("LADDER_ROTATE_MIN_PROFIT_PCT", default=0.8)
        self.rotate_reentry_cooldown_h = env_float("LADDER_ROTATE_REENTRY_COOLDOWN_H", default=12.0)
        self.rotate_grace_near_tp_band_pct = env_float("LADDER_ROTATE_GRACE_NEAR_TP_BAND_PCT", default=0.35)
        self.rotate_grace_near_tp_extra_h = env_float("LADDER_ROTATE_GRACE_NEAR_TP_EXTRA_H", default=6.0)
        self.rotate_grace_momentum_min_pct = env_float("LADDER_ROTATE_GRACE_MOMENTUM_MIN_PCT", default=0.15)
        self.rotate_grace_momentum_lookback = env_int("LADDER_ROTATE_GRACE_MOMENTUM_LOOKBACK", default=30)
        self.rotate_grace_momentum_extra_h = env_float("LADDER_ROTATE_GRACE_MOMENTUM_EXTRA_H", default=3.0)
        self.rotate_grace_max_extend_h = env_float("LADDER_ROTATE_GRACE_MAX_EXTEND_H", default=12.0)

    # ── public API ───────────────────────────────────────────

    def tune(self, market: str, dry_run: bool = False) -> TuneResult:
        analysis = self._analyze_market(market)
        regime = self._classify_regime(market, analysis)
        params = self._calculate_params(regime, analysis)
        params = self._apply_signal_adjustments(params, analysis)
        params = self._apply_performance_feedback(market, params)
        params = self._enforce_profitability_floor(params, analysis)
        manual_lock = False
        try:
            cfg = self._lm.get_config(market)
            manual_lock = str(cfg.get("tune_mode") or "").upper() == "MANUAL"
        except (KeyError, AttributeError, TypeError, ValueError):
            logger.warning("LadderAutoTuner.tune suppressed exception", exc_info=True)
            manual_lock = False
        if manual_lock:
            params.adjustments.append("manual_lock_skip")
        self._log_tune_history(market, regime, params, analysis)
        # [CRASH] Cancel all open buy orders immediately
        if regime == TunerRegime.CRASH and not dry_run:
            try:
                reg = self._lm._read_order_registry()
                m = reg.get(market, {})
                for uuid_, meta in list(m.items()):
                    if isinstance(meta, dict) and meta.get("side") == "buy" and meta.get("status") == "active":
                        try:
                            self._lm._cancel_order(uuid_=uuid_)
                        except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
                            logger.warning("[LADDER_TUNE] CRASH cancel buy order %s: %s", uuid_, exc, exc_info=True)
            except (KeyError, AttributeError, TypeError) as exc:
                logger.warning("[LADDER_TUNE] CRASH cancel buy orders registry: %s", exc, exc_info=True)
        if not dry_run and not manual_lock:
            self._apply_to_config(market, params)
        return params

    def tune_all(self, dry_run: bool = False) -> Dict[str, TuneResult]:
        results: Dict[str, TuneResult] = {}
        try:
            configs = self._lm.list_configs()
        except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
            logger.warning("tune_all: list_configs failed: %s", exc)
            return results
        for cfg in configs:
            mkt = cfg.get("market", "")
            if not mkt or not cfg.get("enabled"):
                continue
            # Rotation is an operational control, not a tuning value.
            # Run it before tuning so timeouts can free the slot immediately.
            if self.rotate_enabled and not dry_run:
                try:
                    rotated = self._maybe_rotate_timeout_market(mkt, cfg)
                    if rotated:
                        continue
                except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
                    logger.warning("rotate_check(%s) failed: %s", mkt, exc)
            try:
                results[mkt] = self.tune(mkt, dry_run=dry_run)
            except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
                logger.error("tune(%s) failed: %s", mkt, exc)
        return results

    def recommend(self, market: str) -> TuneResult:
        """Compute recommendation without applying or logging history."""
        analysis = self._analyze_market(market)
        regime = self._classify_regime(market, analysis)
        params = self._calculate_params(regime, analysis)
        params = self._apply_signal_adjustments(params, analysis)
        params = self._apply_performance_feedback(market, params)
        params = self._enforce_profitability_floor(params, analysis)
        return params

    # ── rotation policy (time + pnl) ────────────────────────

    def _maybe_rotate_timeout_market(self, market: str, cfg: Dict[str, Any]) -> bool:
        system = self._system
        if system is None:
            return False
        coord = getattr(system, "coordinator", None)
        if coord is None:
            return False

        ctx = coord.contexts.get(market)
        if ctx is None:
            return False

        pos = getattr(ctx, "position", None) or {}
        qty = float(pos.get("qty") or 0.0)
        entry = float(pos.get("entry") or 0.0)
        if qty <= 0.0 or entry <= 0.0:
            return False

        now = time.time()
        entry_ts = float(getattr(ctx, "entry_ts", 0.0) or 0.0)
        if entry_ts <= 0.0:
            entry_ts = float(pos.get("ts") or pos.get("entry_ts") or 0.0)
        if entry_ts <= 0.0:
            entry_ts = float(getattr(ctx, "created_at", 0.0) or 0.0)
        if entry_ts <= 0.0:
            return False

        held_sec = max(0.0, now - entry_ts)
        held_h = held_sec / 3600.0

        # Position cycle key (reset one-shot warns when new position is opened)
        cycle_key = f"{market}|{int(entry_ts)}|{entry:.6f}|{qty:.8f}"
        prev_cycle = str(ctx.get_var("ladder_rotate_cycle_key", "") or "")
        if prev_cycle != cycle_key:
            ctx.set_var("ladder_rotate_cycle_key", cycle_key)
            ctx.set_var("ladder_rotate_soft_warned_ts", 0.0)
            ctx.set_var("ladder_rotate_hard_warned_ts", 0.0)
            ctx.set_var("ladder_rotate_grace_sig", "")

        soft_h = max(1.0, float(self.rotate_soft_timeout_h or 24.0))
        profit_h = max(soft_h, float(self.rotate_profit_timeout_h or 48.0))
        hard_h = max(profit_h, float(self.rotate_hard_timeout_h or 72.0))

        from app.core.hyper_price_store import price_store

        current_price = float(price_store.get_price(market) or 0.0)
        if current_price <= 0.0:
            return False
        pnl_pct = ((current_price - entry) / entry) * 100.0

        # "야속한 칼컷" 방지: TP 근접/상승 모멘텀일 때 grace 시간을 자동 부여
        min_profit = float(self.rotate_min_profit_pct or 0.8)
        grace_ext_h = 0.0
        grace_reasons: List[str] = []

        tp_pct = 0.0
        try:
            ctrls = getattr(ctx, "controls", {}) or {}
            params = (((ctrls.get("strategy") or {}).get("params")) or {})
            tp_pct = float(params.get("tp") or 0.0)
        except (KeyError, AttributeError, TypeError, ValueError):
            logger.warning("LadderAutoTuner._maybe_rotate_timeout_market suppressed exception", exc_info=True)
            tp_pct = 0.0

        near_tp_band = max(0.0, float(self.rotate_grace_near_tp_band_pct or 0.0))
        if tp_pct > 0.0:
            near_tp_threshold = max(min_profit, tp_pct - near_tp_band)
            if held_h >= profit_h and pnl_pct >= near_tp_threshold and pnl_pct < tp_pct:
                grace_ext_h += max(0.0, float(self.rotate_grace_near_tp_extra_h or 0.0))
                grace_reasons.append("near_tp")

        mom_pct = self._estimate_short_momentum_pct(
            ctx=ctx,
            lookback=max(5, int(self.rotate_grace_momentum_lookback or 30)),
        )
        if held_h >= profit_h and mom_pct >= float(self.rotate_grace_momentum_min_pct or 0.0):
            grace_ext_h += max(0.0, float(self.rotate_grace_momentum_extra_h or 0.0))
            grace_reasons.append("up_momentum")

        max_ext_h = max(0.0, float(self.rotate_grace_max_extend_h or 0.0))
        grace_ext_h = min(grace_ext_h, max_ext_h)
        profit_h_eff = profit_h + grace_ext_h
        hard_h_eff = hard_h + grace_ext_h

        if grace_ext_h > 0.0 and held_h < hard_h_eff:
            grace_sig = (
                f"{round(grace_ext_h,3)}|{','.join(grace_reasons)}|"
                f"{round(tp_pct,4)}|{round(mom_pct,4)}|{round(pnl_pct,4)}"
            )
            if str(ctx.get_var("ladder_rotate_grace_sig", "") or "") != grace_sig:
                ctx.set_var("ladder_rotate_grace_sig", grace_sig)
                try:
                    system.ledger.append(
                        "LADDER_ROTATE_GRACE_APPLIED",
                        market=market,
                        held_h=round(held_h, 2),
                        pnl_pct=round(pnl_pct, 4),
                        tp_pct=round(tp_pct, 4),
                        momentum_pct=round(mom_pct, 4),
                        extend_h=round(grace_ext_h, 2),
                        reasons=grace_reasons,
                        profit_timeout_h=round(profit_h_eff, 2),
                        hard_timeout_h=round(hard_h_eff, 2),
                    )
                except (TypeError, ValueError) as exc:
                    logger.warning("[LADDER_TUNE] grace extension ledger append: %s", exc, exc_info=True)

        # Soft timeout warning (once per position cycle)
        if held_h >= soft_h:
            soft_warn_ts = float(ctx.get_var("ladder_rotate_soft_warned_ts", 0.0) or 0.0)
            if soft_warn_ts <= 0.0:
                ctx.set_var("ladder_rotate_soft_warned_ts", now)
                try:
                    system.ledger.append(
                        "LADDER_ROTATE_SOFT_TIMEOUT",
                        market=market,
                        held_h=round(held_h, 2),
                        pnl_pct=round(pnl_pct, 4),
                        soft_h=soft_h,
                    )
                except (TypeError, ValueError) as exc:
                    logger.warning("[LADDER_TUNE] soft timeout ledger append: %s", exc, exc_info=True)

        trigger = ""
        if held_h >= hard_h_eff and pnl_pct >= min_profit:
            trigger = "hard_timeout_profit"
        elif held_h >= profit_h_eff and pnl_pct >= min_profit:
            trigger = "profit_timeout"
        elif held_h >= hard_h_eff and pnl_pct < min_profit:
            hard_warn_ts = float(ctx.get_var("ladder_rotate_hard_warned_ts", 0.0) or 0.0)
            if hard_warn_ts <= 0.0:
                ctx.set_var("ladder_rotate_hard_warned_ts", now)
                try:
                    system.ledger.append(
                        "LADDER_ROTATE_HARD_TIMEOUT_HOLD",
                        market=market,
                        held_h=round(held_h, 2),
                        pnl_pct=round(pnl_pct, 4),
                        hard_h=hard_h_eff,
                        min_profit_pct=min_profit,
                    )
                except (TypeError, ValueError) as exc:
                    logger.warning("[LADDER_TUNE] hard timeout hold ledger append: %s", exc, exc_info=True)
            return False
        else:
            return False

        # Prevent repeated rotate requests for same position in a short period.
        last_rotate_ts = float(ctx.get_var("ladder_rotate_last_ts", 0.0) or 0.0)
        if last_rotate_ts > 0.0 and (now - last_rotate_ts) < 300.0:
            return False

        return self._execute_rotate_exit(
            market=market,
            cfg=cfg,
            ctx=ctx,
            trigger=trigger,
            held_h=held_h,
            pnl_pct=pnl_pct,
            current_price=current_price,
            now=now,
        )

    def _execute_rotate_exit(
        self,
        *,
        market: str,
        cfg: Dict[str, Any],
        ctx: Any,
        trigger: str,
        held_h: float,
        pnl_pct: float,
        current_price: float,
        now: float,
    ) -> bool:
        system = self._system
        if system is None:
            return False

        if getattr(ctx, "order_state", None):
            try:
                system.ledger.append(
                    "LADDER_ROTATE_SKIPPED",
                    market=market,
                    trigger=trigger,
                    cause="order_pending",
                )
            except (AttributeError, TypeError) as exc:
                logger.warning("[LADDER_TUNE] rotate_exit skipped ledger append: %s", exc, exc_info=True)
            return False

        # 1) Immediately stop ladder re-seeding and cancel open ladder orders.
        try:
            live_cfg = self._lm.get_config(market)
            if isinstance(live_cfg, dict) and live_cfg:
                live_cfg["enabled"] = False
                self._lm.save_config(live_cfg)
                self._lm.cancel_ladder_orders(live_cfg)
        except (KeyError, AttributeError, TypeError) as exc:
            logger.warning("rotate_exit(%s) config/order cleanup failed: %s", market, exc)

        # 2) Disable strategy controls for this market.
        try:
            ctx.update_controls({"strategy": {"enabled": False}})
        except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
            logger.warning("[LADDER_TUNE] rotate_exit disable strategy controls: %s", exc, exc_info=True)

        # 3) Move market to RECOVERY first to free ACTIVE slot safely.
        try:
            from app.manager.oma_market_registry import MarketState

            system.oma_set_market(
                market=market,
                state=MarketState.RECOVERY,
                reason=["ladder_rotate_timeout", trigger],
            )
        except (KeyError, AttributeError, TypeError) as exc:
            logger.warning("rotate_exit(%s) state transition failed: %s", market, exc)

        # 4) Set local re-entry cooldown to prevent immediate re-pick.
        cooldown_sec = max(0.0, float(self.rotate_reentry_cooldown_h or 0.0) * 3600.0)
        if cooldown_sec > 0.0:
            try:
                prev_until = float(getattr(ctx, "entry_block_until_ts", 0.0) or 0.0)
                new_until = max(prev_until, now + cooldown_sec)
                ctx.entry_block_until_ts = new_until
                ctx.entry_block_reason = "ladder_rotate_cooldown"
            except (KeyError, AttributeError, TypeError, ValueError) as exc:
                logger.warning("[LADDER_TUNE] rotate_exit set cooldown: %s", exc, exc_info=True)

        ctx.set_var("ladder_rotate_last_ts", now)
        ctx.set_var("ladder_rotate_last_trigger", trigger)
        ctx.set_var("ladder_rotate_soft_warned_ts", 0.0)
        ctx.set_var("ladder_rotate_hard_warned_ts", 0.0)
        ctx.set_var("ladder_rotate_grace_sig", "")

        try:
            system._save_context_state()
        except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
            logger.warning("[LADDER_TUNE] rotate_exit save context state: %s", exc, exc_info=True)

        # 5) Request immediate full liquidation (manual reason => LongHold sell-block bypass path).
        try:
            liq = system.request_recovery_liquidate(
                market=market,
                reason=f"manual_ladder_rotate:{trigger}",
            )
        except (AttributeError, TypeError, ValueError) as exc:
            logger.warning("LadderAutoTuner._execute_rotate_exit except: %s", exc, exc_info=True)
            liq = {"ok": False, "error": str(exc)}

        try:
            system.ledger.append(
                "LADDER_ROTATE_EXIT_REQUEST",
                market=market,
                trigger=trigger,
                held_h=round(held_h, 2),
                pnl_pct=round(pnl_pct, 4),
                price=current_price,
                qty=float((getattr(ctx, "position", None) or {}).get("qty") or 0.0),
                result=liq,
                cfg_enabled=bool(cfg.get("enabled")),
            )
        except (KeyError, AttributeError, TypeError, ValueError) as exc:
            logger.warning("[LADDER_TUNE] rotate_exit liquidation request: %s", exc, exc_info=True)

        return bool(liq.get("ok"))

    def _estimate_short_momentum_pct(self, *, ctx: Any, lookback: int) -> float:
        vals: List[float] = []
        try:
            ph = getattr(ctx, "price_history", None)
            if ph is not None:
                vals = [float(x) for x in list(ph)[-max(2, lookback):] if float(x) > 0.0]
        except (KeyError, AttributeError, TypeError, ValueError):
            logger.warning("LadderAutoTuner._estimate_short_momentum_pct suppressed exception", exc_info=True)
            vals = []
        if len(vals) < 2:
            try:
                tail = getattr(ctx, "price_tail", None) or []
                vals = [float(x) for x in list(tail)[-max(2, lookback):] if float(x) > 0.0]
            except (KeyError, AttributeError, TypeError, ValueError):
                logger.warning("LadderAutoTuner._estimate_short_momentum_pct suppressed exception", exc_info=True)
                vals = []
        if len(vals) < 2:
            return 0.0
        start = vals[0]
        end = vals[-1]
        if start <= 0.0:
            return 0.0
        return ((end - start) / start) * 100.0

    # ── market analysis ──────────────────────────────────────

    def _analyze_market(self, market: str) -> MarketAnalysis:
        now = time.time()
        candles_24h = self._load_candles(market, days=1, interval=60)
        candles_7d = self._load_candles(market, days=7, interval=60)

        amp_24h = self._amplitude_pct(candles_24h)
        amp_7d = self._amplitude_pct(candles_7d)
        atr = self._calc_atr_pct(candles_24h)
        vol_trend = self._volume_trend(candles_7d)
        trend_dir = self._trend_direction(candles_7d)
        bounce = self._bounce_rate(candles_7d)
        cur_price = self._last_price(candles_24h)

        fg_value = self._fetch_fear_greed()
        btc_dir, btc_str = self._fetch_btc_signal()
        vs_ratio = self._fetch_volume_spike(market)
        m_regime = self._fetch_market_regime(market)

        return MarketAnalysis(
            market=market,
            amplitude_24h_pct=amp_24h,
            amplitude_7d_pct=amp_7d,
            atr_pct=atr,
            volume_trend=vol_trend,
            trend_direction=trend_dir,
            bounce_rate=bounce,
            current_price=cur_price,
            fear_greed_value=fg_value,
            btc_signal_direction=btc_dir,
            btc_signal_strength=btc_str,
            volume_spike_ratio=vs_ratio,
            market_regime=m_regime,
            timestamp=now,
        )

    # ── regime classification ────────────────────────────────

    def _classify_regime(self, market: str, a: MarketAnalysis) -> TunerRegime:
        if self._detect_crash(a):
            raw = TunerRegime.CRASH
        elif a.amplitude_24h_pct >= 8.0:
            raw = TunerRegime.EXTREME
        elif a.amplitude_24h_pct >= 5.0:
            raw = TunerRegime.ACTIVE
        elif a.amplitude_24h_pct >= 2.0:
            raw = TunerRegime.NORMAL
        else:
            raw = TunerRegime.QUIET

        raw = self._apply_hysteresis(market, raw)
        return raw

    def _detect_crash(self, a: MarketAnalysis) -> bool:
        if a.trend_direction == "down" and a.amplitude_24h_pct >= self.crash_drop_pct:
            return True
        if a.btc_signal_direction == "DOWN" and a.btc_signal_strength >= 0.8:
            if a.amplitude_24h_pct >= self.crash_drop_pct * 0.8:
                return True
        return False

    def _apply_hysteresis(self, market: str, raw: TunerRegime) -> TunerRegime:
        now = time.time()
        prev = self._prev_regime.get(market)
        if prev is None or prev == raw:
            self._prev_regime[market] = raw
            self._regime_since[market] = now
            return raw

        since = self._regime_since.get(market, 0.0)
        if (now - since) < self._hysteresis_sec:
            return prev

        self._prev_regime[market] = raw
        self._regime_since[market] = now
        return raw

    # ── param calculation ────────────────────────────────────

    def _calculate_params(self, regime: TunerRegime, a: MarketAnalysis) -> TuneResult:
        now = time.time()

        if regime == TunerRegime.CRASH:
            return TuneResult(
                market=a.market,
                regime=regime.value,
                step_pct=3.0,
                max_steps=0,
                order_usdt=0,
                martingale=1.0,
                tp_pct=5.0,
                trailing_stop_pct=2.0,
                spacing_value=3.0,
                circuit_breaker=True,
                reason="CRASH detected — all buys blocked",
                adjustments=["circuit_breaker_active"],
                timestamp=now,
            )

        bounds = _REGIME_BOUNDS[regime.value]
        t = self._regime_ratio(a, regime)

        step_pct = self._lerp(bounds["step_pct"], t)
        max_steps = int(self._lerp(bounds["max_steps"], 1.0 - t))
        order_usdt = int(self._lerp(bounds["order_usdt"], t))
        martingale = round(self._lerp(bounds["martingale"], t), 3)
        tp_pct = round(self._lerp(bounds["tp_pct"], t), 3)
        trailing = round(self._lerp(bounds["trailing_stop_pct"], t), 3)

        from app.core.currency import Q
        order_usdt = max(int(Q.min_order), order_usdt)

        return TuneResult(
            market=a.market,
            regime=regime.value,
            step_pct=round(step_pct, 3),
            max_steps=max_steps,
            order_usdt=order_usdt,
            martingale=martingale,
            tp_pct=tp_pct,
            trailing_stop_pct=trailing,
            spacing_value=round(step_pct, 3),
            circuit_breaker=False,
            reason=f"regime={regime.value} amp24h={a.amplitude_24h_pct:.2f}% atr={a.atr_pct:.2f}%",
            adjustments=[],
            timestamp=now,
        )

    def _regime_ratio(self, a: MarketAnalysis, regime: TunerRegime) -> float:
        amp = a.amplitude_24h_pct
        if regime == TunerRegime.QUIET:
            return clamp(amp / 2.0, 0.0, 1.0)
        if regime == TunerRegime.NORMAL:
            return clamp((amp - 2.0) / 3.0, 0.0, 1.0)
        if regime == TunerRegime.ACTIVE:
            return clamp((amp - 5.0) / 3.0, 0.0, 1.0)
        if regime == TunerRegime.EXTREME:
            return clamp((amp - 8.0) / 4.0, 0.0, 1.0)
        return 0.5

    @staticmethod
    def _lerp(bounds: tuple, t: float) -> float:
        lo, hi = bounds
        return lo + (hi - lo) * t

    # ── signal adjustments ───────────────────────────────────

    def _apply_signal_adjustments(self, p: TuneResult, a: MarketAnalysis) -> TuneResult:
        if p.circuit_breaker:
            return p

        bounds = _REGIME_BOUNDS.get(p.regime, _REGIME_BOUNDS["NORMAL"])

        if a.fear_greed_value <= 25:
            p.order_usdt = int(p.order_usdt * 1.3)
            p.adjustments.append("fg_extreme_fear_budget+30%")
        elif a.fear_greed_value >= 75:
            p.order_usdt = int(p.order_usdt * 0.7)
            p.adjustments.append("fg_extreme_greed_budget-30%")

        if a.btc_signal_direction == "DOWN" and a.btc_signal_strength > 0.7:
            p.max_steps = max(2, p.max_steps - 2)
            p.step_pct = round(p.step_pct * 1.2, 3)
            p.adjustments.append("btc_down_defensive")

        if a.volume_spike_ratio >= 3.0:
            if a.trend_direction == "up":
                p.tp_pct = round(p.tp_pct * 1.15, 3)
                p.adjustments.append("vol_spike_bullish_tp+15%")
            elif a.trend_direction == "down":
                p.step_pct = round(p.step_pct * 1.1, 3)
                p.adjustments.append("vol_spike_bearish_step+10%")

        if a.market_regime == "BEAR":
            p.step_pct = round(p.step_pct * 1.2, 3)
            p.martingale = round(max(1.0, p.martingale - 0.05), 3)
            p.adjustments.append("regime_bear_conservative")

        self._clamp_to_bounds(p, bounds)
        return p

    def _apply_performance_feedback(self, market: str, p: TuneResult) -> TuneResult:
        if p.circuit_breaker:
            return p

        bounds = _REGIME_BOUNDS.get(p.regime, _REGIME_BOUNDS["NORMAL"])

        try:
            stats = self._lm.get_market_stats(market)
        except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError):
            logger.warning("LadderAutoTuner._apply_performance_feedback suppressed exception", exc_info=True)
            return p

        buy_c = stats.get("buy_count", 0)
        sell_c = stats.get("sell_count", 0)
        total = buy_c + sell_c
        if total < 5:
            return p

        win_rate = sell_c / total if total > 0 else 0.5
        realized = stats.get("realized_pnl", 0.0)
        fees = stats.get("total_fee", 0.0)
        net = realized - fees

        if win_rate < 0.40:
            p.step_pct = round(p.step_pct * 1.15, 3)
            p.martingale = round(max(1.0, p.martingale - 0.03), 3)
            p.tp_pct = round(p.tp_pct * 1.20, 3)
            p.adjustments.append(f"perf_low_wr={win_rate:.0%}")
        elif win_rate > 0.70:
            p.step_pct = round(p.step_pct * 0.95, 3)
            p.order_usdt = int(p.order_usdt * 1.05)
            p.adjustments.append(f"perf_high_wr={win_rate:.0%}")

        if sell_c > 0 and net < 0:
            avg_loss_per_sell = abs(net) / sell_c
            if avg_loss_per_sell > 0:
                p.tp_pct = round(p.tp_pct * 1.20, 3)
                p.adjustments.append("perf_avg_loss>avg_win_tp+20%")

        self._clamp_to_bounds(p, bounds)
        return p

    # ── profitability floor ──────────────────────────────────

    def _enforce_profitability_floor(self, p: TuneResult, a: MarketAnalysis) -> TuneResult:
        if p.circuit_breaker:
            return p

        slippage_bps = clamp(8 + 6 * a.atr_pct, 8, 25)
        cost_bps = FEE_BPS + slippage_bps + SAFETY_BPS
        tp_min_pct = cost_bps / 100.0
        step_min_pct = max(0.30, cost_bps * 1.2 / 100.0)

        if p.tp_pct < tp_min_pct:
            p.tp_pct = round(tp_min_pct, 3)
            p.adjustments.append(f"floor_tp>={tp_min_pct:.3f}%")
        if p.step_pct < step_min_pct:
            p.step_pct = round(step_min_pct, 3)
            p.adjustments.append(f"floor_step>={step_min_pct:.3f}%")

        p.spacing_value = p.step_pct
        return p

    # ── config apply ─────────────────────────────────────────

    def _apply_to_config(self, market: str, p: TuneResult) -> None:
        try:
            cfg = self._lm.get_config(market)
            if str(cfg.get("tune_mode") or "").upper() == "MANUAL":
                logger.info("Tuner skipped manual config: %s", market)
                return
            cfg["tune_mode"] = "AUTO"
            cfg["spacing_mode"] = "PERCENT"
            cfg["spacing_value"] = p.spacing_value
            cfg["max_levels"] = p.max_steps
            cfg["order_usdt"] = p.order_usdt
            self._lm.save_config(cfg)
            logger.info(
                "Tuner applied %s: regime=%s step=%.3f%% steps=%d order=%d tp=%.3f%%",
                market, p.regime, p.step_pct, p.max_steps, p.order_usdt, p.tp_pct,
            )
        except (KeyError, AttributeError, TypeError, ValueError) as exc:
            logger.error("Tuner apply(%s) failed: %s", market, exc)

    # ── tune history ─────────────────────────────────────────

    def _log_tune_history(
        self, market: str, regime: TunerRegime, p: TuneResult, a: MarketAnalysis,
    ) -> None:
        entry = {
            "ts": time.time(),
            "market": market,
            "regime": regime.value,
            "params": asdict(p),
            "analysis_summary": {
                "amp_24h": a.amplitude_24h_pct,
                "amp_7d": a.amplitude_7d_pct,
                "atr": a.atr_pct,
                "vol_trend": a.volume_trend,
                "trend": a.trend_direction,
                "bounce": a.bounce_rate,
                "price": a.current_price,
                "fg": a.fear_greed_value,
                "btc_dir": a.btc_signal_direction,
                "btc_str": a.btc_signal_strength,
                "vs_ratio": a.volume_spike_ratio,
                "regime_ext": a.market_regime,
            },
        }
        try:
            history = self._read_history()
            history.append(entry)
            if len(history) > TUNE_HISTORY_MAX:
                history = history[-TUNE_HISTORY_MAX:]
            from app.core.io_utils import safe_write_json
            safe_write_json(TUNE_HISTORY_PATH, history, indent=1)
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            logger.warning("tune history write failed: %s", exc)

    def _read_history(self) -> List[Dict[str, Any]]:
        try:
            with open(TUNE_HISTORY_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                return data
        except (FileNotFoundError, json.JSONDecodeError) as exc:
            logger.warning("[LADDER_TUNE] _read_history fallback: %s", exc, exc_info=True)
        return []

    # ── helper: clamp to bounds ──────────────────────────────

    @staticmethod
    def _clamp_to_bounds(p: TuneResult, bounds: Dict[str, tuple]) -> None:
        p.step_pct = round(clamp(p.step_pct, *bounds["step_pct"]), 3)
        p.max_steps = int(clamp(p.max_steps, *bounds["max_steps"]))
        p.order_usdt = int(clamp(p.order_usdt, *bounds["order_usdt"]))
        p.martingale = round(clamp(p.martingale, *bounds["martingale"]), 3)
        p.tp_pct = round(clamp(p.tp_pct, *bounds["tp_pct"]), 3)
        p.trailing_stop_pct = round(
            clamp(p.trailing_stop_pct, *bounds["trailing_stop_pct"]), 3,
        )

    # ── candle helpers ───────────────────────────────────────

    def _load_candles(
        self, market: str, days: int, interval: int,
    ) -> List[Dict[str, Any]]:
        if self._candle_loader is None:
            return []
        try:
            return self._candle_loader.load_candles(
                market, days=days, interval_minutes=interval, max_count=200,
            )
        except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
            logger.warning("candle load(%s, %dd) failed: %s", market, days, exc)
            return []

    @staticmethod
    def _amplitude_pct(candles: List[Dict]) -> float:
        if not candles:
            return 0.0
        highs = [float(c.get("high_price", 0)) for c in candles]
        lows = [float(c.get("low_price", 0)) for c in candles]
        hi = max(highs) if highs else 0.0
        lo = min(lows) if lows else 0.0
        if lo <= 0:
            return 0.0
        return (hi - lo) / lo * 100.0

    @staticmethod
    def _calc_atr_pct(candles: List[Dict]) -> float:
        if not candles:
            return 0.0
        ratios: List[float] = []
        for c in candles:
            h = float(c.get("high_price", 0))
            l = float(c.get("low_price", 0))
            cl = float(c.get("trade_price", 0))
            if cl > 0:
                ratios.append(abs(h - l) / cl * 100.0)
        return sum(ratios) / len(ratios) if ratios else 0.0

    @staticmethod
    def _volume_trend(candles: List[Dict]) -> str:
        if len(candles) < 6:
            return "stable"
        vols = [float(c.get("candle_acc_trade_volume", 0)) for c in candles]
        half = len(vols) // 2
        first_avg = sum(vols[:half]) / half if half else 1.0
        second_avg = sum(vols[half:]) / (len(vols) - half) if (len(vols) - half) else 1.0
        if first_avg <= 0:
            return "stable"
        ratio = second_avg / first_avg
        if ratio > 1.3:
            return "rising"
        if ratio < 0.7:
            return "falling"
        return "stable"

    @staticmethod
    def _trend_direction(candles: List[Dict]) -> str:
        if len(candles) < 4:
            return "sideways"
        prices = [float(c.get("trade_price", 0)) for c in candles if c.get("trade_price")]
        if len(prices) < 4:
            return "sideways"
        first_q = sum(prices[: len(prices) // 4]) / (len(prices) // 4)
        last_q = sum(prices[-(len(prices) // 4):]) / (len(prices) // 4)
        if first_q <= 0:
            return "sideways"
        change = (last_q - first_q) / first_q * 100.0
        if change > 2.0:
            return "up"
        if change < -2.0:
            return "down"
        return "sideways"

    @staticmethod
    def _bounce_rate(candles: List[Dict]) -> float:
        if len(candles) < 3:
            return 0.5
        bounces = 0
        total = 0
        for i in range(1, len(candles) - 1):
            prev_c = float(candles[i - 1].get("trade_price", 0))
            cur_l = float(candles[i].get("low_price", 0))
            cur_c = float(candles[i].get("trade_price", 0))
            if prev_c <= 0 or cur_l <= 0:
                continue
            total += 1
            drop = (prev_c - cur_l) / prev_c * 100.0
            recovery = (cur_c - cur_l) / prev_c * 100.0 if cur_c > cur_l else 0.0
            if drop > 0.3 and recovery > drop * 0.4:
                bounces += 1
        return bounces / total if total > 0 else 0.5

    @staticmethod
    def _last_price(candles: List[Dict]) -> float:
        if not candles:
            return 0.0
        return float(candles[-1].get("trade_price", 0))

    # ── external signal fetchers ─────────────────────────────

    def _fetch_fear_greed(self) -> int:
        if self._fear_greed is None:
            return 50
        try:
            result = self._fear_greed.fetch()
            return result.value
        except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError):
            logger.warning("LadderAutoTuner._fetch_fear_greed suppressed exception", exc_info=True)
            return 50

    def _fetch_btc_signal(self) -> tuple:
        if self._btc_signal is None:
            return ("NEUTRAL", 0.0)
        try:
            sig = self._btc_signal.last_signal
            if sig is None:
                return ("NEUTRAL", 0.0)
            return (sig.direction, sig.strength)
        except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError):
            logger.warning("LadderAutoTuner._fetch_btc_signal suppressed exception", exc_info=True)
            return ("NEUTRAL", 0.0)

    def _fetch_volume_spike(self, market: str) -> float:
        if self._volume_spike is None:
            return 1.0
        try:
            sig = self._volume_spike.recent_signals.get(market)
            if sig is None:
                return 1.0
            return sig.spike_ratio
        except (KeyError, AttributeError, TypeError):
            logger.warning("LadderAutoTuner._fetch_volume_spike suppressed exception", exc_info=True)
            return 1.0

    def _fetch_market_regime(self, market: str) -> str:
        if self._regime_detector is None:
            return "SIDEWAYS"
        try:
            result = self._regime_detector.detect(market)
            return result.regime.value
        except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError):
            logger.warning("LadderAutoTuner._fetch_market_regime suppressed exception", exc_info=True)
            return "SIDEWAYS"
