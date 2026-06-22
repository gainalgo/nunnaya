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
        """섹터 맵 파일에서 코인→섹터 매핑을 로드합니다."""
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

        # WATCH(대기) 상태는 컨텍스트까지 복원하지 않는다.
        # - ACTIVE/RECOVERY + (position/order_state 보유)만 복원하여
        #   부팅 후 "대기 코인 워밍업" 노이즈를 줄이고,
        #   불필요한 warm-up/연산을 피한다.
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
            # [2026-02-02] ACTIVE/RECOVERY 마켓인지 확인하여 is_active 전달
            current_state = self.oma_registry.get_state(market)
            is_active_market = current_state in (MarketState.ACTIVE, MarketState.RECOVERY)
            ctx.apply_state(st, stale_reset_sec=self.context_state_stale_reset_sec, max_prices=self.context_state_max_prices, is_active=is_active_market)

            # 파일이 너무 오래되었으면(강제 stale), warmup 리셋
            # [2026-02-02] ACTIVE/RECOVERY 마켓은 워밍업 리셋 대신 force_ready() 호출
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

            # 복원된 컨텍스트가 position/order를 가지고 있는데 OMA가 ACTIVE가 아니면
            # 안전을 위해 RECOVERY로 승격(진입 금지 + 회수 허용)
            # 단, dust 정리(_dust_disabled 플래그)로 DISABLED된 경우는 재승격 차단
            _restore_dust_disabled = bool(getattr(ctx, '_dust_disabled', False))
            if (has_position or has_order) and (self.oma_registry.get_state(market) != MarketState.ACTIVE) and not _restore_dust_disabled:
                try:
                    # [FIX] 수동 구매 코인: budget_usdt를 실제 포지션 가치로 설정
                    pos_data = st.get("position") or {}
                    pos_entry = float(pos_data.get("entry", 0) or 0)
                    pos_qty = float(pos_data.get("qty", 0) or 0)
                    pos_value = pos_entry * pos_qty if pos_entry > 0 and pos_qty > 0 else None

                    self.oma_registry.set_state(
                        market=market,
                        state=MarketState.RECOVERY,
                        reason=["state_restore", "manual_purchase"],
                        budget_usdt=pos_value,  # 실제 포지션 가치로 예산 설정
                        persist=True
                    )
                except (KeyError, AttributeError, TypeError, ValueError) as exc:
                    try:
                        self.ledger.append("LONGHOLD_SCHEDULE_ERROR", error=str(exc))
                    except (AttributeError, TypeError, ValueError) as exc2:
                        logger.warning("[StateIO] recovery promote ledger error for %s: %s", market, exc2)

            # [FIX] 수동 구매 코인: position이 있으면 allocated_capital을 실제 포지션 가치로 설정
            # 균등 분배된 값이 아니라 실제 구매 금액을 사용해야 다른 코인에 예산이 제대로 분배됨
            if ctx.position:
                pos_usdt = float(ctx.position.get("usdt") or 0)
                if pos_usdt <= 0:
                    # fallback: entry * qty
                    entry = float(ctx.position.get("entry", 0) or 0)
                    qty = float(ctx.position.get("qty", 0) or 0)
                    pos_usdt = entry * qty

                # [PROTECTED] GAZUA 전략은 사용자 설정 예산 우선 - 포지션 가치로 덮어쓰지 않음
                # DO NOT MODIFY: 이 로직은 사용자 지시로 보호됨 (2026-01-23)
                is_gazua = False
                try:
                    s_mode = str(((ctx.controls or {}).get("strategy") or {}).get("mode") or "").upper()
                    is_gazua = (s_mode == "GAZUA")
                except (AttributeError, TypeError, ValueError) as exc:
                    logger.warning("[StateIO] GAZUA check failed for %s: %s", market, exc)

                if is_gazua:
                    # GAZUA: registry 예산이 있으면 유지, 없으면 포지션 가치 사용
                    reg_budget = self.oma_registry.get_budget_usdt(market)
                    if reg_budget and float(reg_budget) > 0:
                        if ctx.allocated_capital <= 0:
                            ctx.allocated_capital = float(reg_budget)
                            try:
                                self.ledger.append("CONTEXT_ALLOC_FIXED", market=market, old=0, new=float(reg_budget), source="gazua_budget")
                            except (TypeError, ValueError) as exc:
                                logger.warning("[StateIO] GAZUA alloc ledger error for %s: %s", market, exc)
                        # allocated가 이미 있으면 유지 (덮어쓰지 않음)
                    elif pos_usdt > 0 and ctx.allocated_capital <= 0:
                        ctx.allocated_capital = pos_usdt
                        try:
                            self.ledger.append("CONTEXT_ALLOC_FIXED", market=market, old=0, new=pos_usdt, source="gazua_pos")
                        except (AttributeError, TypeError) as exc:
                            logger.warning("[StateIO] GAZUA pos alloc ledger error for %s: %s", market, exc)
                else:
                    # GAZUA 외 전략: 사용자 설정 예산 우선
                    # [FIX 2026-01-25] registry에 예산이 있으면 사용자 설정으로 간주하고 유지
                    reg_budget = self.oma_registry.get_budget_usdt(market)
                    if reg_budget and float(reg_budget) > 0:
                        # 사용자가 수동으로 예산을 설정한 경우 → 유지
                        if ctx.allocated_capital <= 0:
                            ctx.allocated_capital = float(reg_budget)
                            try:
                                self.ledger.append("CONTEXT_ALLOC_FIXED", market=market, old=0, new=float(reg_budget), source="user_budget")
                            except (TypeError, ValueError) as exc:
                                logger.warning("[StateIO] user budget alloc ledger error for %s: %s", market, exc)
                        # allocated가 이미 있으면 유지 (덮어쓰지 않음)
                    elif pos_usdt > 0 and ctx.allocated_capital <= 0:
                        # 포지션은 있는데 예산이 없으면 포지션 가치로 복구
                        ctx.allocated_capital = pos_usdt
                        try:
                            self.ledger.append("CONTEXT_ALLOC_FIXED", market=market, old=0, new=pos_usdt, source="pos_fallback")
                        except (AttributeError, TypeError) as exc:
                            logger.warning("[StateIO] pos fallback alloc ledger error for %s: %s", market, exc)

            # [FIX 2026-03-10] controls.strategy.mode가 OMA reason과 불일치하면 복구.
            # 기본값이 mode="PINGPONG", enabled=False 이므로, OMA에 strategy:LADDER 등
            # 다른 전략 태그가 있으면 controls가 제대로 적용 안 된 것이다.
            try:
                ctrls = getattr(ctx, "controls", None) or {}
                strat_cfg = ctrls.get("strategy") or {}
                current_mode = str(strat_cfg.get("mode") or "").strip().upper()

                # OMA reason에서 strategy:XXX 태그 찾기
                oma_reasons = self.oma_registry.get_reason(market) or []
                inferred_strategy = ""
                for r in oma_reasons:
                    if isinstance(r, str) and r.upper().startswith("STRATEGY:"):
                        inferred_strategy = r.split(":", 1)[1].strip().upper()
                        break

                # OMA reason과 현재 mode가 다르면 복구 (기본 PINGPONG→LADDER 등)
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

    # context_state.json 비대화 방지: 이 크기(10MB) 초과 시 .bak 로테이션
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
                    # [2026-03-09] WATCH/DISABLED: price_tail 제외 (설정만 보존, I/O 60% 감소)
                    _is_live = (st in (MarketState.ACTIVE, MarketState.RECOVERY)) or has_pos or has_ord
                    _max_p = self.context_state_max_prices if _is_live else 0
                    out_contexts[m] = ctx.to_state(max_prices=_max_p)

            data = {
                "ts": time.time(),
                "contexts": out_contexts,
            }

            # PID+TID+ns 고유 이름 → 멀티스레드 충돌 및 Windows Defender 잠금 방지
            tmp_path = f"{path}.tmp.{os.getpid()}.{threading.get_ident()}.{time.time_ns()}"
            try:
                with self._state_lock:
                    # Atomic write: tmp → flush/fsync → replace
                    # indent 제거 → 파일 크기 ~40% 감소, 쓰기 속도 향상
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

                    # [2026-03-15] 비대화 방지: 크기 초과 시 .bak 로테이션 + 경고
                    try:
                        file_size = os.path.getsize(path)
                        if file_size > self._CONTEXT_STATE_MAX_BYTES:
                            bak_path = path + ".bak"
                            try:
                                os.replace(path, bak_path)
                            except OSError as exc:
                                logger.error("[StateIO] .bak rotation os.replace failed: %s", exc)
                            logger.warning(
                                "[ContextState] 파일 크기 초과: %s (%.1fMB > %.1fMB) → .bak 로테이션",
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

            # [FIX] guards 안에 flat하게 저장된 경우 fallback
            # reserved/autopilot 섹션이 없으면 guards에서 추출
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
