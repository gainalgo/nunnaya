# ============================================================
# File: app/core/hs_mixin_state_io.py
# Phase 5A: State I/O methods extracted from hyper_system.py
# ============================================================

from __future__ import annotations
import json
import logging
import os
import threading
import time
from typing import Any, Dict, Optional, Set, TYPE_CHECKING

if TYPE_CHECKING:
    pass

from app.manager.oma_market_registry import MarketState

logger = logging.getLogger(__name__)


class StateIOMixin:
    """State persistence mixin (context_state, ui_settings, cooldown).

    Expects (from HyperSystem.__init__):
        self.context_state_path, self.context_state_stale_reset_sec,
        self.context_state_max_prices, self.coordinator, self.oma_registry,
        self.ledger, self._state_lock, self.ui_settings_path,
        self._ui_settings_loaded, self._ui_guard_overrides,
        self.autopilot_cooldown_path, self.autopilot_cooldown,
        self.smart_alloc_sector_map, self.smart_alloc_sector_caps,
        self.smart_alloc_sector_default_cap,
    """

    # --------------------------------------------------------
    # Sector Map Loading
    # --------------------------------------------------------
    def _load_sector_map_from_file(self) -> None:
        """Load the coin->sector mapping from the sector map file."""
        try:
            sector_file = os.path.join(os.path.dirname(__file__), "..", "data", "sector_map.json")
            if not os.path.exists(sector_file):
                return

            with open(sector_file, "r", encoding="utf-8") as f:
                data = json.load(f)

            coin_to_sector = {}
            sector_caps = {}
            for sector_id, sector_info in data.get("sectors", {}).items():
                sector_caps[sector_id] = sector_info.get("cap", 0.40)
                for coin in sector_info.get("coins", []):
                    coin_to_sector[coin] = sector_id

            self.smart_alloc_sector_map = coin_to_sector
            self.smart_alloc_sector_caps = sector_caps
            self.smart_alloc_sector_default_cap = data.get("default_cap", 0.40)
        except (AttributeError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            logger.warning("[StateIO] _load_sector_map_from_file failed: %s", exc, exc_info=True)

    # --------------------------------------------------------
    # Persistence: context_state.json
    # --------------------------------------------------------
    def _load_context_state(self) -> None:
        path = self.context_state_path
        if not path or not os.path.exists(path):
            return

        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            try:
                self.ledger.append("CONTEXT_STATE_LOAD_ERROR", error=str(exc), path=str(path))
            except (AttributeError, TypeError, ValueError) as exc2:
                logger.warning("[StateIO] _load_context_state ledger fallback: %s", exc2)
            return

        ctxs = data.get("contexts")
        if not isinstance(ctxs, dict):
            return

        now = time.time()
        file_ts = 0.0
        try:
            file_ts = float(data.get("ts") or 0.0)
        except (TypeError, ValueError):
            logger.warning("[StateIO] Failed to parse context_state ts, defaulting to 0")
            file_ts = 0.0

        # Do not restore full context for WATCH (idle) markets.
        # - Only restore ACTIVE/RECOVERY + (markets holding a position/order_state)
        #   to reduce post-boot "idle coin warmup" noise and
        #   avoid unnecessary warm-up/computation.
        active_set = set(self.oma_registry.list_active())
        try:
            recovery_list = self.oma_registry.list_recovery() if hasattr(self.oma_registry, "list_recovery") else []
        except (AttributeError, TypeError):
            logger.warning("[StateIO] Failed to get recovery list", exc_info=True)
            recovery_list = []
        recovery_set = set(recovery_list)

        restored = 0
        skipped = 0

        for market, st in ctxs.items():
            if not isinstance(st, dict):
                skipped += 1
                continue

            has_position = isinstance(st.get("position"), dict)
            has_order = isinstance(st.get("order_state"), dict)

            # NOTE
            # - We historically skipped WATCH contexts to reduce warm-up noise.
            # - However, if the operator set manual strategy/guard overrides from the dashboard,
            #   those settings should survive restarts even if the market is not ACTIVE.
            strategy_enabled = False
            guards_overridden = False
            try:
                ctrls = st.get("controls")
                if isinstance(ctrls, dict):
                    sc = ctrls.get("strategy") or {}
                    if isinstance(sc, dict) and bool(sc.get("enabled")):
                        strategy_enabled = True
                    gc = ctrls.get("guards") or {}
                    if isinstance(gc, dict) and len(gc.keys()) > 0:
                        guards_overridden = True
            except (AttributeError, TypeError, ValueError):
                logger.warning("[StateIO] Failed to parse controls for %s", market, exc_info=True)
                strategy_enabled = False
                guards_overridden = False

            needs_restore = (
                (market in active_set)
                or (market in recovery_set)
                or has_position
                or has_order
                or strategy_enabled
                or guards_overridden
            )
            if not needs_restore:
                skipped += 1
                continue

            ctx = self.coordinator.ensure_market(market)
            # [2026-02-02] Check whether this is an ACTIVE/RECOVERY market and pass is_active
            current_state = self.oma_registry.get_state(market)
            is_active_market = current_state in (MarketState.ACTIVE, MarketState.RECOVERY)
            ctx.apply_state(st, stale_reset_sec=self.context_state_stale_reset_sec, max_prices=self.context_state_max_prices, is_active=is_active_market)

            # If the file is too old (force stale), reset warmup
            # [2026-02-02] For ACTIVE/RECOVERY markets call force_ready() instead of warmup reset
            if file_ts > 0 and self.context_state_stale_reset_sec > 0:
                try:
                    if (now - file_ts) > float(self.context_state_stale_reset_sec):
                        if is_active_market:
                            ctx.force_ready()
                        else:
                            ctx.reset_warmup()
                except (TypeError, ValueError) as exc:
                    try:
                        self.ledger.append("LONGHOLD_SCHEDULE_ERROR", error=str(exc))
                    except (AttributeError, TypeError, ValueError) as exc2:
                        logger.warning("[StateIO] warmup reset ledger error for %s: %s", market, exc2)

            # If a restored context holds a position/order but OMA is not ACTIVE,
            # promote it to RECOVERY for safety (no entry allowed + exit/recovery allowed).
            # Exception: if DISABLED via dust cleanup (_dust_disabled flag), block re-promotion.
            _restore_dust_disabled = bool(getattr(ctx, '_dust_disabled', False))
            if (has_position or has_order) and (self.oma_registry.get_state(market) != MarketState.ACTIVE) and not _restore_dust_disabled:
                try:
                    # [FIX] Manually purchased coin: set budget_usdt to the actual position value
                    pos_data = st.get("position") or {}
                    pos_entry = float(pos_data.get("entry", 0) or 0)
                    pos_qty = float(pos_data.get("qty", 0) or 0)
                    pos_value = pos_entry * pos_qty if pos_entry > 0 and pos_qty > 0 else None

                    self.oma_registry.set_state(
                        market=market,
                        state=MarketState.RECOVERY,
                        reason=["state_restore", "manual_purchase"],
                        budget_usdt=pos_value,  # set budget to the actual position value
                        persist=True
                    )
                except (KeyError, AttributeError, TypeError, ValueError) as exc:
                    try:
                        self.ledger.append("LONGHOLD_SCHEDULE_ERROR", error=str(exc))
                    except (AttributeError, TypeError, ValueError) as exc2:
                        logger.warning("[StateIO] recovery promote ledger error for %s: %s", market, exc2)

            # [FIX] Manually purchased coin: if a position exists, set allocated_capital to the actual position value.
            # Use the actual purchase amount (not the evenly-split value) so budget is distributed correctly to other coins.
            if ctx.position:
                pos_usdt = float(ctx.position.get("usdt") or 0)
                if pos_usdt <= 0:
                    # fallback: entry * qty
                    entry = float(ctx.position.get("entry", 0) or 0)
                    qty = float(ctx.position.get("qty", 0) or 0)
                    pos_usdt = entry * qty

                # [PROTECTED] GAZUA strategy prioritizes the user-configured budget - do not overwrite with position value
                # DO NOT MODIFY: this logic is protected per owner instruction (2026-01-23)
                is_gazua = False
                try:
                    s_mode = str(((ctx.controls or {}).get("strategy") or {}).get("mode") or "").upper()
                    is_gazua = (s_mode == "GAZUA")
                except (AttributeError, TypeError, ValueError) as exc:
                    logger.warning("[StateIO] GAZUA check failed for %s: %s", market, exc)

                if is_gazua:
                    # GAZUA: keep the registry budget if present, otherwise use the position value
                    reg_budget = self.oma_registry.get_budget_usdt(market)
                    if reg_budget and float(reg_budget) > 0:
                        if ctx.allocated_capital <= 0:
                            ctx.allocated_capital = float(reg_budget)
                            try:
                                self.ledger.append("CONTEXT_ALLOC_FIXED", market=market, old=0, new=float(reg_budget), source="gazua_budget")
                            except (TypeError, ValueError) as exc:
                                logger.warning("[StateIO] GAZUA alloc ledger error for %s: %s", market, exc)
                        # if allocated already exists, keep it (do not overwrite)
                    elif pos_usdt > 0 and ctx.allocated_capital <= 0:
                        ctx.allocated_capital = pos_usdt
                        try:
                            self.ledger.append("CONTEXT_ALLOC_FIXED", market=market, old=0, new=pos_usdt, source="gazua_pos")
                        except (AttributeError, TypeError) as exc:
                            logger.warning("[StateIO] GAZUA pos alloc ledger error for %s: %s", market, exc)
                else:
                    # Non-GAZUA strategies: prioritize the user-configured budget
                    # [FIX 2026-01-25] if registry has a budget, treat it as user-configured and keep it
                    reg_budget = self.oma_registry.get_budget_usdt(market)
                    if reg_budget and float(reg_budget) > 0:
                        # if the user manually set a budget -> keep it
                        if ctx.allocated_capital <= 0:
                            ctx.allocated_capital = float(reg_budget)
                            try:
                                self.ledger.append("CONTEXT_ALLOC_FIXED", market=market, old=0, new=float(reg_budget), source="user_budget")
                            except (TypeError, ValueError) as exc:
                                logger.warning("[StateIO] user budget alloc ledger error for %s: %s", market, exc)
                        # if allocated already exists, keep it (do not overwrite)
                    elif pos_usdt > 0 and ctx.allocated_capital <= 0:
                        # if a position exists but no budget, restore using the position value
                        ctx.allocated_capital = pos_usdt
                        try:
                            self.ledger.append("CONTEXT_ALLOC_FIXED", market=market, old=0, new=pos_usdt, source="pos_fallback")
                        except (AttributeError, TypeError) as exc:
                            logger.warning("[StateIO] pos fallback alloc ledger error for %s: %s", market, exc)

            # [FIX 2026-03-10] If controls.strategy.mode mismatches the OMA reason, recover it.
            # The default is mode="PINGPONG", enabled=False, so if OMA carries a different
            # strategy tag (e.g. strategy:LADDER), controls were not applied correctly.
            try:
                ctrls = getattr(ctx, "controls", None) or {}
                strat_cfg = ctrls.get("strategy") or {}
                current_mode = str(strat_cfg.get("mode") or "").strip().upper()

                # Find the strategy:XXX tag in the OMA reason
                oma_reasons = self.oma_registry.get_reason(market) or []
                inferred_strategy = ""
                for r in oma_reasons:
                    if isinstance(r, str) and r.upper().startswith("STRATEGY:"):
                        inferred_strategy = r.split(":", 1)[1].strip().upper()
                        break

                # If the OMA reason differs from the current mode, recover it (e.g. default PINGPONG->LADDER)
                if inferred_strategy and inferred_strategy not in ("UNKNOWN", "") and current_mode != inferred_strategy:
                    from app.manager.market_controls import apply_engine_controls
                    apply_engine_controls(self, market, inferred_strategy)
                    logger.info("[BOOT] controls mismatch fixed: %s %s→%s", market, current_mode, inferred_strategy)
            except (KeyError, IndexError, AttributeError, TypeError, ValueError) as exc:
                logger.warning("[StateIO] controls mismatch recovery failed for %s: %s", market, exc)

            restored += 1

        if restored or skipped:
            self.ledger.append(
                "CONTEXT_STATE_LOADED",
                restored=int(restored),
                skipped=int(skipped),
                file_ts=file_ts,
                stale_reset_sec=int(self.context_state_stale_reset_sec),
            )

    # Prevent context_state.json bloat: rotate to .bak when it exceeds this size (10MB)
    _CONTEXT_STATE_MAX_BYTES = 10 * 1024 * 1024  # 10MB

    def _save_context_state(self) -> None:
        path = self.context_state_path
        if not path:
            return

        # Use lock to prevent concurrent writes (WinError 32 fix)
        try:
            d = os.path.dirname(path)
            if d:
                os.makedirs(d, exist_ok=True)

            out_contexts: Dict[str, Any] = {}
            for m, ctx in self.coordinator.get_contexts().items():
                try:
                    st = self.oma_registry.get_state(m)
                except (AttributeError, TypeError):
                    logger.warning("[StateIO] get_state failed for %s, defaulting to DISABLED", m, exc_info=True)
                    st = MarketState.DISABLED

                has_pos = (getattr(ctx, "position", None) is not None)
                has_ord = (getattr(ctx, "order_state", None) is not None)

                strategy_enabled = False
                try:
                    controls = getattr(ctx, "controls", {}) or {}
                    if isinstance(controls, dict):
                        sc = controls.get("strategy", {}) or {}
                        if isinstance(sc, dict) and bool(sc.get("enabled")):
                            strategy_enabled = True
                except (AttributeError, TypeError, ValueError):
                    logger.warning("[StateIO] save: strategy_enabled parse failed for %s", m, exc_info=True)
                    strategy_enabled = False

                guards_overridden = False
                try:
                    controls = getattr(ctx, "controls", {}) or {}
                    if isinstance(controls, dict):
                        gc = controls.get("guards") or {}
                        if isinstance(gc, dict):
                            guards_overridden = any(k for k in gc.keys())
                except (AttributeError, TypeError, ValueError):
                    logger.warning("[StateIO] save: guards_overridden parse failed for %s", m, exc_info=True)
                    guards_overridden = False

                if (st in (MarketState.ACTIVE, MarketState.RECOVERY)) or has_pos or has_ord or strategy_enabled or guards_overridden:
                    # [2026-03-09] WATCH/DISABLED: exclude price_tail (keep settings only, ~60% less I/O)
                    _is_live = (st in (MarketState.ACTIVE, MarketState.RECOVERY)) or has_pos or has_ord
                    _max_p = self.context_state_max_prices if _is_live else 0
                    out_contexts[m] = ctx.to_state(max_prices=_max_p)

            data = {
                "ts": time.time(),
                "contexts": out_contexts,
            }

            # PID+TID+ns unique name -> avoid multithread collisions and Windows Defender locks
            tmp_path = f"{path}.tmp.{os.getpid()}.{threading.get_ident()}.{time.time_ns()}"
            try:
                with self._state_lock:
                    # Atomic write: tmp → flush/fsync → replace
                    # no indent -> ~40% smaller file, faster writes
                    with open(tmp_path, "w", encoding="utf-8") as f:
                        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))

                    # Retry replace for Windows robustness (Antivirus/Indexing race)
                    for attempt in range(5):
                        try:
                            os.replace(tmp_path, path)
                            break
                        except OSError:
                            logger.warning("os.replace retry %d/5 for %s", attempt + 1, path, exc_info=True)
                            if attempt >= 4:
                                raise
                            time.sleep(0.05 * (attempt + 1))

                    # [2026-03-15] Prevent bloat: rotate to .bak + warn when size is exceeded
                    try:
                        file_size = os.path.getsize(path)
                        if file_size > self._CONTEXT_STATE_MAX_BYTES:
                            bak_path = path + ".bak"
                            try:
                                os.replace(path, bak_path)
                            except OSError as exc:
                                logger.error("[StateIO] .bak rotation os.replace failed: %s", exc)
                            logger.warning(
                                "[ContextState] file size exceeded: %s (%.1fMB > %.1fMB) -> .bak rotation",
                                path, file_size / 1024 / 1024,
                                self._CONTEXT_STATE_MAX_BYTES / 1024 / 1024,
                            )
                            self.ledger.append(
                                "CONTEXT_STATE_SIZE_ALERT",
                                size_mb=round(file_size / 1024 / 1024, 1),
                                limit_mb=round(self._CONTEXT_STATE_MAX_BYTES / 1024 / 1024, 1),
                            )
                    except (OSError, TypeError, ValueError) as exc:
                        logger.warning("[StateIO] context_state size check failed: %s", exc)
            finally:
                try:
                    if os.path.exists(tmp_path):
                        os.remove(tmp_path)
                except OSError as exc:
                    logger.warning("[StateIO] tmp file cleanup failed: %s", exc)

        except (OSError, json.JSONDecodeError, KeyError, AttributeError, TypeError, ValueError, OverflowError) as exc:
            try:
                self.ledger.append("CONTEXT_STATE_SAVE_ERROR", error=str(exc), path=str(path))
            except (AttributeError, TypeError, ValueError) as exc2:
                logger.error("[StateIO] context_state save ledger fallback: %s", exc2)
            return

    # --------------------------------------------------------
    # Persistence: ui_settings.json
    # --------------------------------------------------------
    def _load_ui_settings(self) -> None:
        """Load UI overrides from ui_settings.json.

        Global guard + Reserved/Autopilot settings are loaded here.
        Per-market overrides are restored via runtime/context_state.json.
        """
        path = str(getattr(self, "ui_settings_path", "") or "").strip()
        if not path or not os.path.exists(path):
            return

        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            try:
                self.ledger.append("UI_SETTINGS_LOAD_ERROR", error=str(exc), path=str(path))
            except (AttributeError, TypeError, ValueError) as exc2:
                logger.warning("[StateIO] _load_ui_settings ledger fallback: %s", exc2)
            return

        # backward compatible keys
        g = None
        r = None
        ap = None
        if isinstance(data, dict):
            g = data.get("guards")
            if g is None:
                g = data.get("global")
            if g is None and "exit_profit_guard" in data:
                # flat legacy
                g = data

            r = data.get("reserved")
            ap = data.get("autopilot")

            # [FIX] Fallback when settings are stored flat inside guards.
            # If reserved/autopilot sections are missing, extract them from guards.
            if r is None and isinstance(g, dict):
                r = {}
                for k, v in g.items():
                    # reserved_pingpong_n -> pingpong_n
                    if k.startswith("reserved_"):
                        r[k.replace("reserved_", "")] = v
                    elif k in ("pingpong_n", "autoloop_n", "ladder_n", "lightning_n", "gazua_n", "contrarian_n", "sniper_n", "snipers_n", "whale_n", "apply_suggested_budget", "promote_to_active"):
                        r[k] = v
            if ap is None and isinstance(g, dict):
                ap = {}
                for k, v in g.items():
                    # autopilot_auto_approve -> auto_approve
                    if k.startswith("autopilot_"):
                        ap[k.replace("autopilot_", "")] = v

        loaded_any = False

        if isinstance(g, dict):
            self._ui_guard_overrides = dict(g)
            self._ui_apply_guard_settings(self._ui_guard_overrides)
            loaded_any = True

        if isinstance(r, dict) and r:
            self._ui_apply_reserved_settings(r)
            loaded_any = True

        if isinstance(ap, dict) and ap:
            self._ui_apply_autopilot_settings(ap)
            loaded_any = True

        self._ui_settings_loaded = bool(loaded_any)

    def _save_ui_settings(self) -> None:
        from app.core.io_utils import safe_write_json
        path = str(getattr(self, "ui_settings_path", "") or "").strip()
        if not path:
            return

        try:
            data = {
                "ts": time.time(),
                "guards": self._ui_guard_settings_snapshot(),
                "reserved": self._ui_reserved_settings_snapshot(),
                "autopilot": self._ui_autopilot_settings_snapshot(),
            }

            with self._state_lock:
                safe_write_json(path, data)

            # keep a copy in-memory (useful for debugging)
            try:
                self._ui_guard_overrides = dict(data.get("guards") or {})
                self._ui_settings_loaded = True
            except (AttributeError, TypeError, ValueError) as exc:
                logger.warning("[StateIO] ui_settings in-memory copy failed: %s", exc)
        except (OSError, json.JSONDecodeError, KeyError, AttributeError, TypeError, ValueError, OverflowError) as exc:
            try:
                self.ledger.append("UI_SETTINGS_SAVE_ERROR", error=str(exc), path=str(path))
            except (AttributeError, TypeError, ValueError) as exc2:
                logger.error("[StateIO] ui_settings save ledger fallback: %s (original: %s)", exc2, exc)
            return

    def persist_ui_settings(self) -> None:
        """Public helper: persist current global guard settings."""
        try:
            self._save_ui_settings()
        except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
            logger.error("[StateIO] persist_ui_settings failed: %s", exc, exc_info=True)

    # --------------------------------------------------------
    # Autopilot cooldown (avoid re-picking demoted markets too fast)
    # --------------------------------------------------------
    def _load_autopilot_cooldown(self) -> None:
        path = str(getattr(self, 'autopilot_cooldown_path', '') or '').strip()
        if not path or not os.path.exists(path):
            return
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            logger.warning("[StateIO] _load_autopilot_cooldown failed: %s", exc)
            return
        if not isinstance(data, dict):
            return
        m = data.get('markets') if isinstance(data.get('markets'), dict) else data
        if not isinstance(m, dict):
            return
        out: Dict[str, Dict[str, Any]] = {}
        for k, v in m.items():
            mk = str(k or '').strip().upper()
            if not mk:
                continue
            if isinstance(v, dict):
                until_ts = v.get('until_ts')
                reason = v.get('reason')
            else:
                until_ts = v
                reason = ''
            try:
                until_f = float(until_ts or 0.0)
            except (TypeError, ValueError):
                logger.warning("[StateIO] cooldown load: until_ts parse error for %s", mk)
                until_f = 0.0
            out[mk] = {
                'until_ts': until_f,
                'reason': str(reason or ''),
                'ts': float(data.get('ts') or 0.0) if isinstance(data, dict) else 0.0,
            }
        self.autopilot_cooldown = out

    def _save_autopilot_cooldown(self) -> None:
        from app.core.io_utils import safe_write_json
        path = str(getattr(self, 'autopilot_cooldown_path', '') or '').strip()
        if not path:
            return
        try:
            payload = {
                'ts': time.time(),
                'markets': dict(getattr(self, 'autopilot_cooldown', {}) or {}),
            }
            with self._state_lock:
                safe_write_json(path, payload)
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            logger.error("[StateIO] _save_autopilot_cooldown failed: %s", exc)
            return

    def _autopilot_cooldown_prune(self, *, now_ts: Optional[float] = None) -> None:
        now_ts = float(now_ts or time.time())
        m = dict(getattr(self, 'autopilot_cooldown', {}) or {})
        if not m:
            return
        changed = False
        for mk in list(m.keys()):
            try:
                until_ts = float((m.get(mk) or {}).get('until_ts') or 0.0)
            except (AttributeError, TypeError, ValueError):
                logger.warning("[StateIO] cooldown prune: parse error for %s", mk, exc_info=True)
                until_ts = 0.0
            if until_ts and until_ts <= now_ts:
                m.pop(mk, None)
                changed = True
        if changed:
            self.autopilot_cooldown = m
            try:
                self._save_autopilot_cooldown()
            except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
                logger.warning("[StateIO] _autopilot_cooldown_prune save failed: %s", exc)

    def get_autopilot_cooldown_markets(self, *, now_ts: Optional[float] = None) -> Set[str]:
        now_ts = float(now_ts or time.time())
        out: Set[str] = set()
        m = getattr(self, 'autopilot_cooldown', {}) or {}
        if not isinstance(m, dict):
            return out
        for mk, v in m.items():
            try:
                until_ts = float((v or {}).get('until_ts') or 0.0) if isinstance(v, dict) else float(v or 0.0)
            except (AttributeError, TypeError, ValueError):
                logger.warning("[StateIO] cooldown markets: parse error for %s", mk, exc_info=True)
                until_ts = 0.0
            if until_ts and until_ts > now_ts:
                out.add(str(mk).strip().upper())
        return out

    def _autopilot_cooldown_mark(self, market: str, *, minutes: Optional[int] = None, reason: str = '') -> None:
        market = str(market or '').strip().upper()
        if not market:
            return
        try:
            mins = int(minutes) if minutes is not None else int(getattr(self, 'autopilot_cooldown_min', 0) or 0)
        except (AttributeError, TypeError, ValueError):
            logger.warning("[StateIO] cooldown_mark: minutes parse error for %s", market, exc_info=True)
            mins = int(getattr(self, 'autopilot_cooldown_min', 0) or 0)
        mins = max(0, mins)
        if mins <= 0:
            return
        now_ts = time.time()
        until_ts = float(now_ts + (mins * 60))
        m = dict(getattr(self, 'autopilot_cooldown', {}) or {})
        prev_until = 0.0
        try:
            prev_until = float((m.get(market) or {}).get('until_ts') or 0.0)
        except (AttributeError, TypeError, ValueError):
            logger.warning("[StateIO] cooldown_mark: prev_until parse error for %s", market, exc_info=True)
            prev_until = 0.0
        # extend only
        if prev_until and prev_until > until_ts:
            until_ts = prev_until
        m[market] = {
            'until_ts': until_ts,
            'reason': str(reason or ''),
            'ts': now_ts,
        }
        self.autopilot_cooldown = m
        try:
            self._save_autopilot_cooldown()
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            logger.warning("[STATE_IO] extend only: %s", exc, exc_info=True)
