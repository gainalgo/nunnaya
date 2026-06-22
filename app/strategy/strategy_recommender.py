# ============================================================
# File: app/strategy/strategy_recommender.py
# Autocoin OS v3-H — Strategy Recommendation Helper
# ------------------------------------------------------------
# Computes recommended params from recent candle stats.
# ============================================================

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from app.backtest.candle_loader import CandleLoader
from app.manager.ladder_auto_tuner import LadderAutoTuner
from app.core.currency import Q

logger = logging.getLogger(__name__)

clamp = lambda x, lo, hi: max(lo, min(hi, x))


class StrategyRecommender:
    def __init__(self, system: Any | None = None) -> None:
        self._system = system
        self._candle_loader = CandleLoader()

    def recommend(
        self,
        strategy: str,
        market: str,
        budget_usdt: Optional[float] = None,
    ) -> Dict[str, Any]:
        strat = (strategy or "").strip().upper()
        mkt = (market or "").strip().upper()
        if not strat or not mkt:
            raise ValueError("strategy and market are required")

        analysis = self._analyze_market(mkt)
        params: Dict[str, Any] = {}
        budget_rec: Optional[int] = None

        if strat == "LADDER":
            params, budget_rec = self._recommend_ladder(mkt)
        elif strat == "LIGHTNING":
            params = self._recommend_lightning(analysis)
            budget_rec = self._recommend_budget(budget_usdt, analysis["volatility_pct"])
        elif strat == "GAZUA":
            params = self._recommend_gazua(analysis)
            budget_rec = self._recommend_budget(budget_usdt, analysis["volatility_pct"])
        elif strat == "CONTRARIAN":
            params = self._recommend_contrarian(analysis)
            budget_rec = self._recommend_budget(budget_usdt, analysis["volatility_pct"])
        elif strat in ("SNIPER", "SNIPERS"):
            params = self._recommend_sniper(analysis)
            if strat == "SNIPERS":
                # SNIPER(s): 고정 순환 프로필 기본값
                params["profile"] = "SNIPERS"
                params["side"] = "LONG"
                params["cycle_mode"] = "UP"
                params["auto_reentry"] = True
                params["no_demote"] = False
            budget_rec = self._recommend_budget(budget_usdt, analysis["volatility_pct"])
        else:
            raise ValueError(f"unsupported strategy: {strat}")

        return {
            "strategy": strat,
            "market": mkt,
            "params": params,
            "budget_usdt": budget_rec,
            "analysis": analysis,
        }

    # --- analysis -----------------------------------------------------

    def _analyze_market(self, market: str) -> Dict[str, Any]:
        candles_24h = self._load_candles(market, days=1)
        candles_7d = self._load_candles(market, days=7)

        atr_pct = LadderAutoTuner._calc_atr_pct(candles_24h)
        amp_24h = LadderAutoTuner._amplitude_pct(candles_24h)
        amp_7d = LadderAutoTuner._amplitude_pct(candles_7d)
        trend = LadderAutoTuner._trend_direction(candles_7d)
        last_price = LadderAutoTuner._last_price(candles_24h) or LadderAutoTuner._last_price(candles_7d)

        # Volatility proxy (pct)
        vol = max(atr_pct, amp_24h / 4.0, 0.5)

        return {
            "atr_pct": round(atr_pct, 3),
            "amp_24h_pct": round(amp_24h, 3),
            "amp_7d_pct": round(amp_7d, 3),
            "trend": trend,
            "last_price": last_price,
            "volatility_pct": round(vol, 3),
        }

    def _load_candles(self, market: str, days: int) -> list[Dict[str, Any]]:
        try:
            return self._candle_loader.load_candles(market, days=days, interval_minutes=60, max_count=200)
        except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
            logger.warning("recommend: candle load(%s, %dd) failed: %s", market, days, exc)
            return []

    # --- recommendations ---------------------------------------------

    def _recommend_ladder(self, market: str) -> tuple[Dict[str, Any], Optional[int]]:
        try:
            mgr = getattr(self._system, "ladder_manager", None) if self._system else None
            if mgr is None and self._system is not None:
                from app.manager.ladder_manager import LadderManager
                mgr = LadderManager(self._system)
                self._system.ladder_manager = mgr
            if mgr is None:
                raise RuntimeError("ladder_manager unavailable")

            tuner = LadderAutoTuner(mgr, system=self._system)
            p = tuner.recommend(market)
            params = {
                "step_pct": round(float(p.step_pct), 3),
                "max_steps": int(p.max_steps),
                "order_usdt": int(p.order_usdt),
                "martingale": round(float(p.martingale), 3),
                "tp_pct": round(float(p.tp_pct), 3),
            }
            budget_rec = int(p.order_usdt) * int(p.max_steps) if p.order_usdt and p.max_steps else None
            return params, budget_rec
        except (KeyError, AttributeError, TypeError, ValueError) as exc:
            logger.warning("recommend ladder fallback(%s): %s", market, exc)
            params = {
                "step_pct": 1.0,
                "max_steps": 10,
                "order_usdt": max(int(Q.min_order), 10),
                "martingale": 1.0,
                "tp_pct": 2.0,
            }
            budget_rec = params["order_usdt"] * params["max_steps"]
            return params, budget_rec

    def _recommend_lightning(self, a: Dict[str, Any]) -> Dict[str, Any]:
        vol = float(a.get("volatility_pct") or 1.0)
        tp = clamp(round(vol * 1.6, 2), 2.0, 6.0)
        sl = -clamp(round(vol * 0.9, 2), 1.0, 4.0)
        return {"tp_pct": tp, "sl_pct": sl}

    def _recommend_gazua(self, a: Dict[str, Any]) -> Dict[str, Any]:
        vol = float(a.get("volatility_pct") or 1.0)
        tp = clamp(round(vol * 4.0, 2), 10.0, 30.0)
        sl = -clamp(round(vol * 2.5, 2), 8.0, 25.0)
        trail = clamp(round(vol * 1.2, 2), 2.0, 6.0)
        return {"tp": tp, "sl": sl, "trail_tp": True, "trail_dist_pct": trail}

    def _recommend_contrarian(self, a: Dict[str, Any]) -> Dict[str, Any]:
        vol = float(a.get("volatility_pct") or 1.0)
        trend = str(a.get("trend") or "sideways")
        tp = 15.0
        sl = -50.0
        min_score = 3 if trend == "down" else 2
        cooldown = 900 if vol >= 2.5 else 600
        return {
            "tp_pct": tp,
            "sl_pct": sl,
            "trail_tp": False,
            "trail_dist_pct": 0.3,
            "min_score": min_score,
            "cooldown_sec": cooldown,
        }

    def _recommend_sniper(self, a: Dict[str, Any]) -> Dict[str, Any]:
        vol = float(a.get("volatility_pct") or 1.0)
        tp = clamp(round(vol * 1.2, 2), 1.2, 4.0)
        sl = clamp(round(vol * 0.8, 2), 2.5, 5.0)
        entry_thr = clamp(round(vol * 0.3, 2), 0.2, 0.8)
        exit_thr = clamp(round(vol * 0.25, 2), 0.2, 0.6)
        trail = clamp(round(vol * 0.6, 2), 0.8, 2.0)
        return {
            "tp_pct": tp,
            "sl_pct": sl,
            "entry_threshold_pct": entry_thr,
            "exit_threshold_pct": exit_thr,
            "trail_tp": vol >= 1.5,
            "trail_dist_pct": trail,
        }

    # --- budget -------------------------------------------------------

    def _recommend_budget(self, base_budget: Optional[float], vol_pct: float) -> Optional[int]:
        if base_budget is None:
            return None
        try:
            base = float(base_budget)
        except (TypeError, ValueError):
            logger.warning("[Recommender] base_budget 파싱 실패", exc_info=True)
            return None
        if base <= 0:
            return None
        scale = clamp(2.5 / max(vol_pct, 0.5), 0.5, 1.0)
        rec = int(round(base * scale / 1000.0)) * 1000
        rec = max(rec, int(Q.min_order))
        return rec
