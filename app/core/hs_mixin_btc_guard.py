"""Phase 5G – BTC Guard mixin extracted from hyper_system.py."""

from __future__ import annotations
import logging
import time
from typing import Any, Dict, Optional

from app.core.constants import (
    BYBIT_MARKET_TICKERS,
    bybit_v5_rest_category,
    parse_bybit_list,
    normalize_bybit_ticker,
)
from app.core.currency import Q

logger = logging.getLogger(__name__)


class BtcGuardMixin:
    """BTC Guard mode, trailing-stop tightening, and recovery boost."""

    async def _check_btc_guard_mode(self) -> None:
        """
        [2026-02-06] Periodic BTC Guard Mode check

        On BTC decline:
        - btc_guard_mode = True
        - disable all auto_approve except CONTRARIAN
        - previous state saved in _pre_guard_auto_approve

        On BTC recovery:
        - btc_guard_mode = False
        - restore saved auto_approve state
        """
        if not self.btc_guard_enabled:
            return

        from app.monitor.btc_leading_signal import (
            get_btc_leading_detector,
            initialize_btc_leading_detector,
        )
        detector = get_btc_leading_detector()
        if detector is None:
            try:
                class _BybitTickerClient:
                    @staticmethod
                    def get_ticker(market_code: str) -> Optional[Dict[str, Any]]:
                        try:
                            from app.core.rate_limiter import bybit_get
                            market_norm = Q.normalize(market_code)
                            resp = bybit_get(
                                BYBIT_MARKET_TICKERS,
                                params={"category": bybit_v5_rest_category()},
                                timeout=1.5,
                            )
                            if resp.status_code != 200:
                                return None
                            for _t in parse_bybit_list(resp.json()):
                                if isinstance(_t, dict):
                                    _tc = normalize_bybit_ticker(_t)
                                    if _tc.get("market", "").upper() == market_norm.upper():
                                        return _tc
                        except (ConnectionError, TimeoutError, OSError) as e:
                            logger.warning("[BtcGuard] ticker fetch failed for %s: %s", market_code, e)
                            return None
                        except Exception:
                            logger.warning("[BtcGuard] ticker fetch unexpected error for %s", market_code, exc_info=True)
                            return None
                        return None

                detector = initialize_btc_leading_detector(_BybitTickerClient())
                self.ledger.append("BTC_GUARD_DETECTOR_INIT", ok=bool(detector))
            except Exception as init_exc:
                logger.warning("[BTC_GUARD] detector init error: %s", init_exc, exc_info=True)
                self.ledger.append("BTC_GUARD_DETECTOR_INIT_ERROR", error=str(init_exc))
                return

        try:
            # Keep detector thresholds aligned with runtime guard settings.
            detector.threshold_5m = max(0.5, float(self.btc_guard_down_5m_pct))
            detector.threshold_15m = max(1.0, float(self.btc_guard_down_15m_pct))
        except (AttributeError, TypeError, ValueError) as exc:
            logger.warning("[BTC_GUARD] threshold sync error: %s", exc, exc_info=True)

        try:
            signal = detector.detect_signal()
            if not signal:
                return

            strength_threshold = float(self.btc_guard_threshold)
            down_5m = float(self.btc_guard_down_5m_pct)
            down_15m = float(self.btc_guard_down_15m_pct)
            up_5m = float(self.btc_guard_recover_5m_pct)
            up_15m = float(self.btc_guard_recover_15m_pct)

            btc_5m = float(getattr(signal, "btc_change_5m", 0.0) or 0.0)
            btc_15m = float(getattr(signal, "btc_change_15m", 0.0) or 0.0)

            down_by_strength = signal.direction == "DOWN" and float(signal.strength) > strength_threshold
            down_by_pct = (btc_5m <= -down_5m) or (btc_15m <= -down_15m)
            up_by_strength = signal.direction == "UP" and float(signal.strength) > strength_threshold
            up_by_pct = (btc_5m >= up_5m) or (btc_15m >= up_15m)

            # Decline signal detected → activate Guard
            if down_by_strength or down_by_pct:
                if not self.btc_guard_mode:
                    # save previous state
                    self._pre_guard_auto_approve = {
                        "pingpong": self.autopilot_auto_approve_pingpong,
                        "autoloop": self.autopilot_auto_approve_autoloop,
                        "ladder": self.autopilot_auto_approve_ladder,
                        "lightning": self.autopilot_auto_approve_lightning,
                        "gazua": self.autopilot_auto_approve_gazua,
                        "sniper": self.autopilot_auto_approve_sniper,
                    }

                    # if Recovery Boost is on, deactivate it first
                    if self.recovery_boost_active:
                        self._deactivate_recovery_boost(reason="btc_guard_reactivated")

                    # activate Guard (block all except CONTRARIAN)
                    self.btc_guard_mode = True
                    self.autopilot_auto_approve_pingpong = False
                    self.autopilot_auto_approve_autoloop = False
                    self.autopilot_auto_approve_ladder = False
                    self.autopilot_auto_approve_lightning = False
                    self.autopilot_auto_approve_gazua = False
                    self.autopilot_auto_approve_sniper = False
                    # CONTRARIAN is kept as-is

                    # [2026-02-06] Tighten Trailing Stop (prevent further decline)
                    self._tighten_trailing_stops()

                    self.ledger.append(
                        "BTC_GUARD_ACTIVATED",
                        btc_change_5m=btc_5m,
                        btc_change_15m=btc_15m,
                        strength=signal.strength,
                        confidence=signal.confidence,
                        reason="strength_or_pct",
                        down_5m_pct=down_5m,
                        down_15m_pct=down_15m,
                    )

            # Recovery signal detected → deactivate Guard
            elif up_by_strength or up_by_pct:
                if self.btc_guard_mode:
                    # restore previous state
                    self.autopilot_auto_approve_pingpong = self._pre_guard_auto_approve.get("pingpong", False)
                    self.autopilot_auto_approve_autoloop = self._pre_guard_auto_approve.get("autoloop", False)
                    self.autopilot_auto_approve_ladder = self._pre_guard_auto_approve.get("ladder", False)
                    self.autopilot_auto_approve_lightning = self._pre_guard_auto_approve.get("lightning", False)
                    self.autopilot_auto_approve_gazua = self._pre_guard_auto_approve.get("gazua", False)
                    self.autopilot_auto_approve_sniper = self._pre_guard_auto_approve.get("sniper", False)

                    self.btc_guard_mode = False
                    self._pre_guard_auto_approve = {}

                    # [2026-02-06] Restore Trailing Stop
                    self._restore_trailing_stops()

                    # [2026-03-18] Recovery Boost — fast recovery + extra profit on rebound
                    self._activate_recovery_boost(btc_5m=btc_5m, btc_15m=btc_15m)

                    self.ledger.append(
                        "BTC_GUARD_DEACTIVATED",
                        btc_change_5m=btc_5m,
                        btc_change_15m=btc_15m,
                        strength=signal.strength,
                        confidence=signal.confidence,
                        reason="strength_or_pct",
                        recover_5m_pct=up_5m,
                        recover_15m_pct=up_15m,
                        recovery_boost=bool(self.recovery_boost_active),
                    )

        except (KeyError, AttributeError, TypeError, ValueError) as e:
            self.ledger.append("BTC_GUARD_CHECK_ERROR", error=str(e))

    def _tighten_trailing_stops(self) -> None:
        """
        [2026-02-06] Tighten Trailing Stop when BTC Guard activates

        Purpose: prevent further decline during a BTC crash
        - shrink Trailing Stop of all ACTIVE markets to 0.5x (0.3% → 0.15%)
        - original values are saved and restored when Guard is deactivated
        """
        if not hasattr(self, '_pre_guard_trailing_stops'):
            self._pre_guard_trailing_stops = {}

        try:
            active_markets = self.oma_registry.list_active()
            count = 0

            for market in active_markets:
                try:
                    ctx = self.coordinator.get_context(market)
                    if not ctx:
                        continue

                    originals: Dict[str, float] = {}
                    ratio = float(getattr(self, "btc_guard_trail_tighten_ratio", 0.5) or 0.5)

                    # Strategy controls trailing params
                    ctrls = getattr(ctx, "controls", None) or {}
                    if isinstance(ctrls, dict):
                        st = ctrls.get("strategy", {}) if isinstance(ctrls.get("strategy"), dict) else {}
                        sp = st.get("params", {}) if isinstance(st.get("params"), dict) else {}
                        for key in ("trail_dist_pct", "trailing_stop_pct"):
                            if key in sp:
                                try:
                                    orig = float(sp.get(key) or 0.0)
                                    if orig > 0:
                                        originals[f"controls:{key}"] = orig
                                        sp[key] = max(0.05, orig * ratio)
                                except (TypeError, ValueError) as exc:
                                    logger.warning("[BTC_GUARD] strategy trailing param error: %s", exc, exc_info=True)

                    # Engine policy trailing params
                    pol = getattr(ctx, "policy", None) or {}
                    pp = pol.get("params", {}) if isinstance(pol, dict) and isinstance(pol.get("params"), dict) else {}
                    for key in ("trail_dist_pct", "trailing_stop_pct"):
                        if key in pp:
                            try:
                                orig = float(pp.get(key) or 0.0)
                                if orig > 0:
                                    originals[f"policy:{key}"] = orig
                                    pp[key] = max(0.05, orig * ratio)
                            except (TypeError, ValueError) as exc:
                                logger.warning("[BTC_GUARD] policy trailing param error: %s", exc, exc_info=True)

                    if originals:
                        self._pre_guard_trailing_stops[market] = originals
                        if hasattr(ctx, "update_policy") and isinstance(pol, dict):
                            try:
                                ctx.update_policy(pol)
                            except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
                                logger.warning("[BTC_GUARD] update_policy error during tighten: %s", exc, exc_info=True)
                        count += 1

                except (KeyError, AttributeError, TypeError, ValueError) as exc:
                    logger.warning("[BTC_GUARD] tighten trailing stop error for market: %s", exc, exc_info=True)
                    continue

            if count > 0:
                self.ledger.append(
                    "BTC_GUARD_TRAILING_TIGHTENED",
                    count=count,
                    ratio=float(getattr(self, "btc_guard_trail_tighten_ratio", 0.5) or 0.5),
                )

        except (KeyError, AttributeError, TypeError, ValueError) as e:
            self.ledger.append("BTC_GUARD_TRAILING_ERROR", error=str(e), phase="tighten")

    def _restore_trailing_stops(self) -> None:
        """
        [2026-02-06] Restore Trailing Stop when BTC Guard deactivates

        Restore to the saved original Trailing Stop values
        """
        if not hasattr(self, '_pre_guard_trailing_stops'):
            return

        try:
            count = 0

            for market, original_map in self._pre_guard_trailing_stops.items():
                try:
                    ctx = self.coordinator.get_context(market)
                    if not ctx:
                        continue

                    if not isinstance(original_map, dict):
                        continue

                    restored = 0

                    ctrls = getattr(ctx, "controls", None) or {}
                    if isinstance(ctrls, dict):
                        st = ctrls.get("strategy", {}) if isinstance(ctrls.get("strategy"), dict) else {}
                        sp = st.get("params", {}) if isinstance(st.get("params"), dict) else {}
                        for key in ("trail_dist_pct", "trailing_stop_pct"):
                            ref_key = f"controls:{key}"
                            if ref_key in original_map and isinstance(sp, dict):
                                try:
                                    sp[key] = float(original_map[ref_key])
                                    restored += 1
                                except (TypeError, ValueError) as exc:
                                    logger.warning("[BTC_GUARD] restore controls trailing param error: %s", exc, exc_info=True)

                    pol = getattr(ctx, "policy", None) or {}
                    pp = pol.get("params", {}) if isinstance(pol, dict) and isinstance(pol.get("params"), dict) else {}
                    for key in ("trail_dist_pct", "trailing_stop_pct"):
                        ref_key = f"policy:{key}"
                        if ref_key in original_map and isinstance(pp, dict):
                            try:
                                pp[key] = float(original_map[ref_key])
                                restored += 1
                            except (TypeError, ValueError) as exc:
                                logger.warning("[BTC_GUARD] restore policy trailing param error: %s", exc, exc_info=True)
                    if restored > 0 and hasattr(ctx, "update_policy") and isinstance(pol, dict):
                        try:
                            ctx.update_policy(pol)
                        except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
                            logger.warning("[BTC_GUARD] update_policy error during restore: %s", exc, exc_info=True)
                        count += 1

                except (KeyError, AttributeError, TypeError, ValueError) as exc:
                    logger.warning("[BTC_GUARD] restore trailing stop error for market: %s", exc, exc_info=True)
                    continue

            # reset saved state
            self._pre_guard_trailing_stops = {}

            if count > 0:
                self.ledger.append("BTC_GUARD_TRAILING_RESTORED", count=count)

        except (KeyError, AttributeError, TypeError, ValueError) as e:
            self.ledger.append("BTC_GUARD_TRAILING_ERROR", error=str(e), phase="restore")

    def _activate_recovery_boost(self, btc_5m: float = 0.0, btc_15m: float = 0.0) -> None:
        """Activate Recovery Boost when a rebound is detected after a decline.

        Losing position → lower TP to exit quickly at breakeven+α (fill the gap)
        Winning position + momentum → raise TP to chase extra profit
        """
        if not self.recovery_boost_enabled or self.recovery_boost_active:
            return
        try:
            now = time.time()
            self.recovery_boost_active = True
            self.recovery_boost_activated_ts = now
            self._pre_boost_tp = {}

            quick_tp = float(self.recovery_boost_quick_tp_pct)
            momentum_mult = float(self.recovery_boost_momentum_tp_mult)
            boosted = 0
            extended = 0

            from app.core.hyper_price_store import price_store

            for market, ctx in (self.coordinator.contexts or {}).items():
                try:
                    pos = getattr(ctx, "position", None) or {}
                    entry = float(pos.get("entry") or 0.0)
                    pos_qty = float(pos.get("qty") or 0.0)
                    if entry <= 0 or pos_qty <= 0:
                        continue

                    cur_price = float(price_store.get_price(market) or 0.0)
                    if cur_price <= 0:
                        continue
                    change_pct = ((cur_price - entry) / entry) * 100.0

                    ctrls = getattr(ctx, "controls", None) or {}
                    st = ctrls.get("strategy", {}) if isinstance(ctrls.get("strategy"), dict) else {}
                    sp = st.get("params", {}) if isinstance(st.get("params"), dict) else {}
                    cur_tp = float(sp.get("tp", sp.get("tp_pct", 0.0)) or 0.0)

                    if cur_tp <= 0:
                        continue

                    self._pre_boost_tp[market] = {"tp": cur_tp, "change_pct": change_pct}

                    if change_pct < 0:
                        breakeven_tp = abs(change_pct) + quick_tp + 0.15
                        new_tp = max(quick_tp, min(breakeven_tp, cur_tp))
                        sp["tp"] = round(new_tp, 4)
                        if "tp_pct" in sp:
                            sp["tp_pct"] = round(new_tp, 4)
                        boosted += 1
                    elif change_pct > 0.5:
                        new_tp = round(cur_tp * momentum_mult, 4)
                        sp["tp"] = new_tp
                        if "tp_pct" in sp:
                            sp["tp_pct"] = new_tp
                        extended += 1

                except (KeyError, AttributeError, TypeError, ValueError) as exc:
                    logger.warning("[BTC_GUARD] recovery boost position error: %s", exc, exc_info=True)
                    continue

            self.ledger.append(
                "RECOVERY_BOOST_ACTIVATED",
                boosted_quick=boosted,
                extended_momentum=extended,
                total_positions=len(self._pre_boost_tp),
                quick_tp_pct=quick_tp,
                momentum_tp_mult=momentum_mult,
                budget_mult=float(self.recovery_boost_budget_mult),
                btc_5m=btc_5m,
                btc_15m=btc_15m,
                duration_sec=float(self.recovery_boost_duration_sec),
            )
            logger.info(
                "[RECOVERY_BOOST] ON — quick:%d extended:%d total:%d duration:%.0fs",
                boosted, extended, len(self._pre_boost_tp), self.recovery_boost_duration_sec,
            )
        except (OSError, KeyError, AttributeError, TypeError, ValueError, OverflowError) as e:
            logger.warning("[RECOVERY_BOOST] activation error: %s", e)
            self.recovery_boost_active = False

    def _deactivate_recovery_boost(self, reason: str = "expired") -> None:
        """Deactivate Recovery Boost — restore original TP."""
        if not self.recovery_boost_active:
            return
        try:
            restored = 0
            for market, saved in self._pre_boost_tp.items():
                try:
                    ctx = self.coordinator.get_context(market)
                    if not ctx:
                        continue
                    ctrls = getattr(ctx, "controls", None) or {}
                    st = ctrls.get("strategy", {}) if isinstance(ctrls.get("strategy"), dict) else {}
                    sp = st.get("params", {}) if isinstance(st.get("params"), dict) else {}
                    orig_tp = float(saved.get("tp", 0.0))
                    if orig_tp > 0:
                        sp["tp"] = orig_tp
                        if "tp_pct" in sp:
                            sp["tp_pct"] = orig_tp
                        restored += 1
                except (KeyError, AttributeError, TypeError, ValueError) as exc:
                    logger.warning("[BTC_GUARD] recovery boost deactivate position error: %s", exc, exc_info=True)
                    continue

            self.ledger.append(
                "RECOVERY_BOOST_DEACTIVATED",
                reason=reason,
                restored=restored,
                duration_actual_sec=round(time.time() - self.recovery_boost_activated_ts, 1),
            )
            logger.info("[RECOVERY_BOOST] OFF — reason:%s restored:%d", reason, restored)
        except (OSError, KeyError, AttributeError, TypeError, ValueError, OverflowError) as e:
            logger.warning("[RECOVERY_BOOST] deactivation error: %s", e)
        finally:
            self.recovery_boost_active = False
            self.recovery_boost_activated_ts = 0.0
            self._pre_boost_tp = {}

    def _check_recovery_boost_expiry(self) -> None:
        """Check Recovery Boost time expiry — called from tick_loop."""
        if not self.recovery_boost_active:
            return
        try:
            elapsed = time.time() - self.recovery_boost_activated_ts
            if elapsed >= self.recovery_boost_duration_sec:
                self._deactivate_recovery_boost(reason="expired")
        except (OSError, TypeError, ValueError, OverflowError) as exc:
            logger.warning("[BTC_GUARD] recovery boost expiry check error: %s", exc, exc_info=True)
