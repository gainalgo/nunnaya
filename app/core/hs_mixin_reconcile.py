"""Phase 5C mixin -- reconcile / recovery / dust methods."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Dict, Any, Optional, List, Tuple

from app.manager.oma_market_registry import MarketState
from app.core.currency import Q
from app.core.hyper_price_store import price_store
from app.core.constants import BYBIT_MARKET_TICKERS, bybit_v5_rest_category, env_bool as _env_bool

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# TYPE_CHECKING imports (avoid circular / heavy imports at runtime)
# ---------------------------------------------------------------------------
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from app.engine.hyper_engine_context import HyperEngineContext


class ReconcileMixin:
    """Reconcile, recovery-policy, emergency-stop, and dust-cleanup helpers."""

    # ------------------------------------------------------------------
    # Boot validation
    # ------------------------------------------------------------------
    def _boot_validate_positions(self) -> None:
        """부팅 후 검증 훅.

        reconcile + SNIPER restore 이후에도 position qty > 0인데
        strategy.mode가 없거나 비어있는 마켓을 찾아 자동 정합화한다.
        - sniper_store에 있으면 → SNIPER 적용
        - 없으면 → GAZUA 적용
        """
        try:
            from app.manager.sniper_position_store import sniper_store
            from app.manager.market_controls import apply_engine_controls
            sniper_markets: set = set()
            try:
                sniper_markets = {
                    v.get("market", "").upper()
                    for v in sniper_store.get_all().values()
                    if v.get("market")
                }
            except (KeyError, AttributeError, TypeError, ValueError):
                logger.warning("[Boot] SNIPER 포지션 목록 조회 실패 — SNIPER 포지션이 GAZUA로 강제 지정될 수 있음", exc_info=True)

            # ★ FOCUS 포지션 마켓 수집 — GAZUA 강탈 방지
            focus_markets: set = set()
            try:
                fm = getattr(self, "focus_manager", None)
                if fm and getattr(fm, "enabled", False) and fm.positions:
                    focus_markets = {p.market.upper() for p in fm.positions if p.market}
                    if focus_markets:
                        logger.info("[Boot] FOCUS markets excluded from validation: %s", focus_markets)
            except Exception:
                pass

            # ★ Cross-strategy overlap 감지: 부팅 시 양쪽 다 보유 중인 마켓 정리
            fixed = 0
            contexts = getattr(self.coordinator, "contexts", {}) or {}
            try:
                nunnaya_markets: set = set()
                for _m, _ctx in list(contexts.items()):
                    _pos = getattr(_ctx, "position", None) or {}
                    if float(_pos.get("qty", 0) or 0) > 0:
                        nunnaya_markets.add(_m.upper())
                _overlaps = focus_markets & nunnaya_markets
                if _overlaps:
                    logger.error("[Boot] CROSS-STRATEGY OVERLAP DETECTED: %s — FOCUS owns, clearing Nunnaya side", _overlaps)
                    self.ledger.append("CROSS_STRATEGY_OVERLAP_BOOT", markets=list(_overlaps))
                    for _om in _overlaps:
                        _octx = contexts.get(_om)
                        if _octx and getattr(_octx, "position", None):
                            _octx.position = None
                            logger.warning("[Boot] Cleared Nunnaya position for %s (FOCUS priority)", _om)
            except Exception as exc:
                logger.debug("[Boot] cross-strategy overlap check: %s", exc)

            for market, ctx in list(contexts.items()):
                try:
                    pos = getattr(ctx, "position", None) or {}
                    qty = float(pos.get("qty", 0) or 0)
                    if qty <= 0:
                        continue
                    # ★ FOCUS 소유 마켓은 건드리지 않음
                    if market.upper() in focus_markets:
                        logger.info("[Boot] %s belongs to FOCUS — skipping validation", market)
                        continue
                    ctrls = getattr(ctx, "controls", {}) or {}
                    s_mode = str((ctrls.get("strategy") or {}).get("mode") or "").strip().upper()
                    if s_mode:
                        continue  # 전략 모드 정상 — 건드리지 않음
                    # 모드 없음 → 정합화
                    if market in sniper_markets:
                        apply_engine_controls(self, market, "SNIPER")
                        self.ledger.append("BOOT_VALIDATE_FIX", market=market, assigned="SNIPER")
                    else:
                        apply_engine_controls(self, market, "GAZUA")
                        self.ledger.append("BOOT_VALIDATE_FIX", market=market, assigned="GAZUA")
                    fixed += 1
                except (KeyError, AttributeError, TypeError, ValueError) as exc:
                    self.ledger.append("BOOT_VALIDATE_SKIP", market=market, error=str(exc))

            if fixed > 0:
                self.ledger.append("BOOT_VALIDATE_DONE", fixed=fixed)
        except (KeyError, AttributeError, TypeError, ValueError) as exc:
            self.ledger.append("BOOT_VALIDATE_ERROR", error=str(exc))

    # ------------------------------------------------------------------
    # Equity estimation
    # ------------------------------------------------------------------
    def _estimate_equity_from_accounts(self, accounts: List[Dict[str, Any]]) -> Tuple[float, float, float]:
        """accounts() 기반으로 USDT cash / deployed / equity를 추정한다.

        - cash_usdt: USDT balance+locked
        - deployed_usdt: 비USDT 보유(locked 포함) * (현재가 또는 avg_buy_price)
        - equity_usdt: cash_usdt + deployed_usdt
        """
        cash_usdt = 0.0
        deployed_usdt = 0.0

        for a in accounts:
            try:
                cur = str(a.get("currency") or "").upper()
                bal = float(a.get("balance") or 0.0)
                locked = float(a.get("locked") or 0.0)
                qty = bal + locked

                if cur == Q.symbol:  # USDT (quote currency)
                    cash_usdt = float(qty)
                    continue

                if qty <= 0:
                    continue

                market = Q.market(cur)
                price = price_store.get_price(market)
                if not price:
                    try:
                        ab = float(a.get("avg_buy_price") or 0.0)
                        if ab > 0:
                            price = ab
                    except (TypeError, ValueError):
                        logger.warning("[reconcile] avg_buy_price parse error for equity estimate", exc_info=True)
                        price = None

                if price and float(price) > 0:
                    val = float(qty) * float(price)
                    if val < 1.0:          # dust coin < $1 → 무시
                        continue
                    deployed_usdt += val

            except (AttributeError, TypeError, ValueError) as exc:
                logger.error("[equity_estimation] per-account skip: %s", exc, exc_info=True)
                continue

        equity_usdt = cash_usdt + deployed_usdt
        return float(cash_usdt), float(deployed_usdt), float(equity_usdt)

    # ------------------------------------------------------------------
    # Reconcile  (~946 lines)
    # ------------------------------------------------------------------
    def reconcile(self, *, reason: str = "manual") -> Dict[str, Any]:
        """Bybit 계정 상태를 기반으로 orphan/대기주문/포지션을 점검."""
        # NOTE: reconcile is a global operation, so we lock to prevent race with oma_set_market/purge
        if not self.trade_client:
            return {"ok": True, "mode": self.trading_mode, "reason": reason,
            "position_sync_mode": "OFF",
            "position_sync": {"synced": 0, "cleared": 0}, "skipped": True}

        # cancel wait orders on boot 옵션
        if reason == "boot" and self.cancel_wait_orders_on_boot:
            try:
                waits = self.trade_client.list_wait_orders(max_pages=3)
                cancelled = 0
                for o in waits:
                    oid = str(o.get("uuid") or "")
                    mk = str(o.get("market") or "")
                    if not oid:
                        continue
                    try:
                        self.trade_client.cancel_order(uuid=oid)
                        cancelled += 1
                        self.ledger.append("BOOT_CANCEL_WAIT_ORDER", market=mk, uuid=oid)
                    except Exception as exc:
                        self.ledger.append("BOOT_CANCEL_WAIT_ORDER_ERROR", market=mk, uuid=oid, error=str(exc))
                if cancelled:
                    self.ledger.append("BOOT_CANCEL_WAIT_ORDER_SUMMARY", cancelled=cancelled)
            except Exception as exc:
                self.ledger.append("BOOT_CANCEL_WAIT_ORDER_ERROR", error=str(exc), phase="list")

        # accounts
        # -----------------------------------------------------------------
        # TradeClient.accounts() signature compatibility
        # Some clients may accept skip_currencies; we support both without breaking.
        # -----------------------------------------------------------------
        try:
            accounts = self.trade_client.accounts(skip_currencies=self.skip_currencies)  # type: ignore[arg-type]
        except TypeError:
            logger.warning("[reconcile] trade_client.accounts() skip_currencies not supported, falling back", exc_info=True)
            accounts = self.trade_client.accounts()

        # Client-agnostic filtering
        if self.skip_currencies:
            skip = set(self.skip_currencies)
            accounts = [a for a in accounts if (a.get("currency") not in skip)]
        accounts = list(accounts or [])
        prev_accounts_snapshot = list(getattr(self, "_accounts_snapshot", []) or [])
        prev_cash_usdt = float(getattr(self, "_last_cash_usdt", 0.0) or 0.0)
        prev_deployed_usdt = float(getattr(self, "_last_deployed_usdt", 0.0) or 0.0)
        prev_equity_usdt = float(getattr(self, "_last_equity_usdt", 0.0) or 0.0)

        cash_usdt, deployed_usdt, equity_usdt = self._estimate_equity_from_accounts(accounts)

        # Guard against transient account snapshot glitches:
        # If equity suddenly collapses to near-zero, retry once and keep previous snapshot for this cycle.
        try:
            suspicious_low_equity = (
                self.trading_mode != "PAPER"
                and prev_equity_usdt >= 50.0
                and float(equity_usdt) < 1.0
            )
            if suspicious_low_equity:
                self.ledger.append(
                    "EQUITY_SNAPSHOT_LOW_DETECTED",
                    reason=reason,
                    prev_equity_usdt=float(prev_equity_usdt),
                    new_equity_usdt=float(equity_usdt),
                    accounts_n=len(accounts),
                )

                # Retry once with fresh account snapshot
                try:
                    retry_accounts = self.trade_client.accounts(skip_currencies=self.skip_currencies)  # type: ignore[arg-type]
                except TypeError:
                    logger.warning("[reconcile] retry accounts() skip_currencies not supported, falling back", exc_info=True)
                    retry_accounts = self.trade_client.accounts()

                if self.skip_currencies:
                    skip = set(self.skip_currencies)
                    retry_accounts = [a for a in retry_accounts if (a.get("currency") not in skip)]

                retry_accounts = list(retry_accounts or [])
                cash2, dep2, eq2 = self._estimate_equity_from_accounts(retry_accounts)

                if float(eq2) >= 1000.0:
                    accounts = retry_accounts
                    cash_usdt, deployed_usdt, equity_usdt = cash2, dep2, eq2
                    self.ledger.append(
                        "EQUITY_SNAPSHOT_RETRY_OK",
                        reason=reason,
                        prev_equity_usdt=float(prev_equity_usdt),
                        retry_equity_usdt=float(eq2),
                        accounts_n=len(retry_accounts),
                    )
                else:
                    # Keep previous values for this cycle to avoid accidental budget collapse.
                    if prev_accounts_snapshot:
                        accounts = prev_accounts_snapshot
                    cash_usdt, deployed_usdt, equity_usdt = (
                        float(prev_cash_usdt),
                        float(prev_deployed_usdt),
                        float(prev_equity_usdt),
                    )
                    self.ledger.append(
                        "EQUITY_SNAPSHOT_FALLBACK_PREV",
                        reason=reason,
                        prev_equity_usdt=float(prev_equity_usdt),
                        retry_equity_usdt=float(eq2),
                        retry_accounts_n=len(retry_accounts),
                    )
        except (KeyError, AttributeError, TypeError, ValueError) as exc:
            logger.error("[EQUITY_SNAPSHOT] equity safeguard check FAILED — using raw values: %s", exc, exc_info=True)
            # 에러 발생 시 raw equity 값(line 182)은 이미 계산됨 → 그대로 사용
            # stale 값으로 덮어쓰면 예산 붕괴 위험이 더 큼

        self._accounts_snapshot = list(accounts or [])
        self._last_cash_usdt = cash_usdt
        self._last_deployed_usdt = deployed_usdt
        self._last_equity_usdt = equity_usdt
        self._last_equity_ts = time.time()

        # Auto-slot recalculation on equity change
        if getattr(self, "auto_slot_enabled", False):
            try:
                prev_auto_eq = float(getattr(self, "_auto_slot_last_equity", 0.0) or 0.0)
                new_eq = float(equity_usdt)
                if new_eq >= 1.0 and (prev_auto_eq <= 0 or abs(new_eq - prev_auto_eq) / max(prev_auto_eq, 1.0) > 0.10):
                    from app.manager.auto_slot_allocator import compute_auto_slots
                    slots = compute_auto_slots(new_eq)
                    _slot_map = {
                        "pingpong_n": "reserved_pingpong_n",
                        "autoloop_n": "reserved_autoloop_n",
                        "ladder_n": "reserved_ladder_n",
                        "lightning_n": "reserved_lightning_n",
                        "gazua_n": "reserved_gazua_n",
                        "contrarian_n": "reserved_contrarian_n",
                        "sniper_n": "reserved_sniper_n",
                        "whale_n": "reserved_whale_n",
                    }
                    for key, attr in _slot_map.items():
                        setattr(self, attr, int(slots.get(key, 0)))
                    self._auto_slot_last_equity = new_eq
                    self.persist_ui_settings()
            except Exception as exc:
                logger.warning("[AUTO_SLOT] recalculation failed: %s", exc)

        with self._lock:
            # -----------------------------
            # Drawdown guard (optional safety)
            # -----------------------------
            # - 계정 전체 손실이 임계치를 넘으면 자동으로 BUY를 차단(쿨다운)하거나
            #   RECOVERY/EMERGENCY_STOP으로 전환한다.
            try:
                self._check_drawdown_guard(equity_usdt=float(equity_usdt), reason=f"reconcile:{reason}")
            except (TypeError, ValueError) as exc:
                logger.error("[drawdown_guard] check failed: %s", exc, exc_info=True)


            # orphan detection + position sync
            orphans: List[Dict[str, Any]] = []
            promoted: List[str] = []
            synced: List[Dict[str, Any]] = []
            cleared: List[str] = []

            sync_mode = str(getattr(self, "reconcile_position_sync_mode", "OFF") or "OFF").strip().upper()
            if sync_mode not in ("OFF", "ACTIVE", "ALL"):
                sync_mode = "OFF"

            skip_ccy = set([str(c).upper() for c in (self.skip_currencies or [])])

            # Helper: fetch price from Bybit V5 REST API as fallback
            def _fetch_bybit_price(market: str) -> Optional[float]:
                try:
                    from app.core.rate_limiter import bybit_get
                    from app.core.constants import parse_bybit_list, normalize_bybit_ticker
                    bybit_market = Q.normalize(market)
                    resp = bybit_get(
                        BYBIT_MARKET_TICKERS, params={"category": bybit_v5_rest_category()}, timeout=5.0
                    )
                    if resp.status_code == 200:
                        for _t in parse_bybit_list(resp.json()):
                            if isinstance(_t, dict):
                                _tc = normalize_bybit_ticker(_t)
                                if _tc.get("market", "").upper() == bybit_market.upper():
                                    return float(_tc.get("trade_price") or 0.0)
                except (ConnectionError, TimeoutError, OSError) as exc:
                    logger.warning("[RECONCILE] _fetch_bybit_price network error for %s: %s", market, exc)
                except Exception as exc:
                    logger.error("[RECONCILE] _fetch_bybit_price FAILED for %s: %s", market, exc, exc_info=True)
                return None

            # Build holdings map from accounts (balance + locked)
            holdings: Dict[str, Dict[str, Any]] = {}
            for a in accounts:
                try:
                    cur = str(a.get("currency") or "").upper()
                    if cur in ("USDT", "USDC") or not cur:
                        continue
                    if cur in skip_ccy:
                        continue
                    bal = float(a.get("balance") or 0.0)
                    locked = float(a.get("locked") or 0.0)
                    qty = bal + locked
                    if qty <= 0:
                        continue
                    try:
                        avg_buy = float(a.get("avg_buy_price") or 0.0)
                    except (TypeError, ValueError):
                        logger.warning("[reconcile] avg_buy_price parse error in holdings scan", exc_info=True)
                        avg_buy = 0.0
                    market = Q.market(cur)

                    # 🧹 Dust filter: min_order 미만 잔액은 무시
                    # Try price_store first, then Bybit REST API as fallback
                    price = price_store.get_price(market)
                    if price is None or price <= 0:
                        price = _fetch_bybit_price(market)
                    if price is None or price <= 0:
                        price = avg_buy
                    price = float(price or 0.0)

                    value_usdt = qty * price if price else 0.0
                    _dust_threshold = float(getattr(self, 'min_order_usdt', Q.min_order) or Q.min_order)
                    if value_usdt < _dust_threshold:
                        continue

                    if self._known_markets and market not in self._known_markets:
                        self.ledger.append("ORPHAN_UNKNOWN_MARKET", market=market, currency=cur, qty=float(qty))
                        continue
                    holdings[market] = {"currency": cur, "qty": float(qty), "avg_buy_price": float(avg_buy), "price": float(price)}
                except (AttributeError, TypeError, ValueError) as exc:
                    logger.error("[holdings_price] price lookup failed: %s", exc, exc_info=True)
                    continue

            def _sync_ctx_position(market: str, ctx, qty: float, avg_buy: float, *, source: str) -> None:
                # Avoid fighting with our own live order state machine
                if getattr(ctx, "order_state", None):
                    return

                prev_qty = 0.0
                prev_entry = 0.0
                if isinstance(getattr(ctx, "position", None), dict):
                    try:
                        prev_qty = float(ctx.position.get("qty") or 0.0)
                    except (TypeError, ValueError):
                        logger.warning("[reconcile] prev_qty parse error for position sync", exc_info=True)
                        prev_qty = 0.0
                    try:
                        prev_entry = float(ctx.position.get("entry") or 0.0)
                    except (TypeError, ValueError):
                        logger.warning("[reconcile] prev_entry parse error for position sync", exc_info=True)
                        prev_entry = 0.0

                qty_tol = 1e-12
                entry_tol = 1e-8

                # Clear (manual sell / external close)
                if qty <= qty_tol:
                    if ctx.position is not None:
                        ctx.position = None
                        cleared.append(market)
                        self.ledger.append(
                            "POSITION_SYNC_CLEAR",
                            market=market,
                            prev_qty=float(prev_qty),
                            prev_entry=float(prev_entry),
                            source=source,
                        )
                        # [FIX 2026-03-23] 수동 매도/외부 청산 시 longhold config 자동 정리
                        # 포지션이 사라졌는데 longhold_config에 남아있으면
                        # 슬롯 차감 + LONGHOLD_SELL_BLOCKED 유령 이벤트 발생
                        try:
                            _lm = getattr(self, "ladder_manager", None)
                            if _lm:
                                _lh_cfg = _lm.get_longhold_config(market)
                                if _lh_cfg and _lh_cfg.get("enabled"):
                                    _lm.remove_longhold_config(market)
                                    logger.info("[reconcile] LongHold config auto-removed: %s (position cleared)", market)
                        except (KeyError, AttributeError, TypeError):
                            logger.debug("[reconcile] longhold cleanup skip: %s", market, exc_info=True)
                        # [FIX 2026-03-23] Ladder grid config 자동 비활성화
                        try:
                            _lm2 = getattr(self, "ladder_manager", None)
                            if _lm2:
                                _ld_cfg = _lm2.get_config(market)
                                if isinstance(_ld_cfg, dict) and _ld_cfg.get("enabled"):
                                    _ld_cfg["enabled"] = False
                                    _lm2.save_config(_ld_cfg)
                                    logger.info("[reconcile] Ladder config auto-disabled: %s (position cleared)", market)
                        except (KeyError, AttributeError, TypeError):
                            logger.debug("[reconcile] ladder cleanup skip: %s", market, exc_info=True)
                    # [FIX 2026-03-24] 포지션 없어도 ACTIVE면 registry budget으로 예산 복구
                    if ctx.allocated_capital <= 0:
                        try:
                            _reg_b = self.oma_registry.get_budget_usdt(market)
                            if _reg_b and float(_reg_b) > 0:
                                ctx.allocated_capital = float(_reg_b)
                                ctx.usable_capital = float(_reg_b)
                        except (AttributeError, TypeError, ValueError) as exc:
                            logger.error("[budget_recovery] registry budget restore failed: %s", exc, exc_info=True)
                    return

                # Determine entry (with Bybit REST API fallback)
                if avg_buy and avg_buy > 0:
                    entry = float(avg_buy)
                else:
                    entry = price_store.get_price(market)
                    if entry is None or entry <= 0:
                        entry = _fetch_bybit_price(market)
                    if entry is None or entry <= 0:
                        entry = prev_entry
                    entry = float(entry or 0.0)

                changed = False
                if ctx.position is None:
                    changed = True
                else:
                    if abs(prev_qty - qty) > max(qty_tol, qty_tol * max(prev_qty, qty, 1.0)):
                        changed = True
                    if entry and abs(prev_entry - entry) > entry_tol:
                        changed = True

                # [FIX] allocated_capital 동기화는 position 변경 여부와 관계없이 항상 체크
                # 외부 구매로 포지션 가치가 allocated보다 클 때 동기화
                pos_value = (float(entry) * float(qty)) if entry else 0.0
                if pos_value > 0 or ctx.allocated_capital <= 0:
                    old_alloc = ctx.allocated_capital

                    # [PROTECTED] GAZUA 전략은 사용자 설정 예산 우선 - 포지션 가치로 덮어쓰지 않음
                    # DO NOT MODIFY: 이 로직은 사용자 지시로 보호됨 (2026-01-23)
                    is_gazua = False
                    try:
                        s_mode = str(((ctx.controls or {}).get("strategy") or {}).get("mode") or "").upper()
                        is_gazua = (s_mode == "GAZUA")
                    except (AttributeError, TypeError, ValueError) as exc:
                        logger.error("[capital_sync] GAZUA capital sync failed: %s", exc, exc_info=True)

                    # GAZUA: OMA Registry에 budget_usdt가 있으면 그 값 유지
                    if is_gazua:
                        reg_budget = self.oma_registry.get_budget_usdt(market)
                        if reg_budget and float(reg_budget) > 0:
                            # 사용자 설정 예산이 있으면 덮어쓰지 않음
                            if old_alloc <= 0:
                                ctx.allocated_capital = float(reg_budget)
                                self.ledger.append("ALLOCATED_SYNC", market=market, old=old_alloc, new=float(reg_budget), source="gazua_budget_restore")
                            # 이미 allocated가 있으면 유지
                        else:
                            # registry에 예산 없으면 포지션 가치로 설정 (신규 등록 케이스)
                            if old_alloc < pos_value - 100:
                                ctx.allocated_capital = pos_value
                                self.ledger.append("ALLOCATED_SYNC", market=market, old=old_alloc, new=pos_value, source=source)
                    else:
                        # GAZUA 외 전략: 사용자 설정 예산 우선
                        # [FIX 2026-01-25] registry에 예산이 있으면 사용자 설정으로 간주하고 유지
                        reg_budget_other = self.oma_registry.get_budget_usdt(market)
                        if reg_budget_other and float(reg_budget_other) > 0:
                            # 사용자가 수동으로 예산을 설정한 경우 → 유지
                            if old_alloc <= 0:
                                ctx.allocated_capital = float(reg_budget_other)
                                self.ledger.append("ALLOCATED_SYNC", market=market, old=old_alloc, new=float(reg_budget_other), source="user_budget")
                            # 이미 allocated가 있으면 유지 (덮어쓰지 않음)
                        elif old_alloc <= 0 and pos_value > 0:
                            # 예산 설정 없고 포지션만 있으면 포지션 가치로 복구
                            ctx.allocated_capital = pos_value
                            self.ledger.append("ALLOCATED_SYNC", market=market, old=old_alloc, new=pos_value, source="pos_fallback")

                if not changed:
                    return

                # [FIX 2026-02-19] 기존 position 필드 보존 (entry_ts, cost/fee 등)
                # reconcile은 qty/entry만 동기화하고 fill-tracking 필드는 유지
                if ctx.position is not None:
                    ctx.position["entry"] = float(entry) if entry else 0.0
                    ctx.position["qty"] = float(qty)
                    ctx.position["usdt"] = pos_value
                    ctx.position["principal_usdt"] = pos_value
                    ctx.position["source"] = source
                    ctx.position["ts"] = time.time()
                    if not ctx.position.get("entry_ts"):
                        ctx.position["entry_ts"] = time.time()
                else:
                    ctx.position = {
                        "entry": float(entry) if entry else 0.0,
                        "qty": float(qty),
                        "usdt": pos_value,
                        "principal_usdt": pos_value,
                        "source": source,
                        "ts": time.time(),
                        "entry_ts": time.time(),
                    }

                synced.append({"market": market, "qty": float(qty), "entry": float(entry) if entry else 0.0, "source": source})
                self.ledger.append("POSITION_SYNC", market=market, qty=float(qty), entry=float(entry) if entry else 0.0, source=source)

            # Process holdings
            for market, info in holdings.items():
                try:
                    qty = float(info.get("qty") or 0.0)
                    if qty <= 0:
                        continue
                    cur = str(info.get("currency") or "")
                    avg_buy = float(info.get("avg_buy_price") or 0.0)

                    ctx = self.coordinator.ensure_market(market)
                    ctx.trading_mode = self.trading_mode
                    ctx.market_state = self.oma_registry.get_state(market).value

                    manual_ctl = (ctx.controls or {}).get("manual") or {}
                    manual_enabled = bool(manual_ctl.get("enabled"))

                    # Manual mode: never promote to RECOVERY; always sync position for UI/monitoring
                    if manual_enabled:
                        _sync_ctx_position(market, ctx, qty, avg_buy, source="manual")
                        # [FIX] manual 코인도 allocated 동기화 필요
                        if qty > 0 and avg_buy > 0:
                            pos_value = float(qty) * float(avg_buy)
                            if pos_value > 0 and abs(ctx.allocated_capital - pos_value) > 100:
                                old_alloc = ctx.allocated_capital
                                ctx.allocated_capital = pos_value
                                self.ledger.append("ALLOCATED_SYNC", market=market, old=old_alloc, new=pos_value, source="manual")
                        continue

                    # Position sync (optional)
                    if sync_mode == "ALL":
                        _sync_ctx_position(market, ctx, qty, avg_buy, source="reconcile:all")
                    elif sync_mode == "ACTIVE" and self.oma_registry.get_state(market) == MarketState.ACTIVE:
                        # [PATCH] Force sync for GAZUA strategy (Manual Buy mode) even if manual_enabled is False
                        # Gazua는 사용자가 앱에서 직접 매수하는 것이 기본이므로,
                        # 외부 잔고 변동을 항상 즉시 반영해야 합니다.
                        controls = getattr(ctx, "controls", None) or {}
                        strategy = controls.get("strategy") if isinstance(controls, dict) else {}
                        s_mode = str((strategy or {}).get("mode") or "").upper() if isinstance(strategy, dict) else ""
                        is_gazua = (s_mode == "GAZUA")

                        if is_gazua:
                            _sync_ctx_position(market, ctx, qty, avg_buy, source="reconcile:gazua")
                            # [FIX] GAZUA 코인도 allocated 동기화 필요
                            if qty > 0 and avg_buy > 0:
                                pos_value = float(qty) * float(avg_buy)
                                if pos_value > 0 and abs(ctx.allocated_capital - pos_value) > 100:
                                    old_alloc = ctx.allocated_capital
                                    ctx.allocated_capital = pos_value
                                    self.ledger.append("ALLOCATED_SYNC", market=market, old=old_alloc, new=pos_value, source="gazua")
                            continue

                        _sync_ctx_position(market, ctx, qty, avg_buy, source="reconcile:active")
                    elif sync_mode == "ACTIVE" and self.oma_registry.get_state(market) == MarketState.RECOVERY:
                        # [FIX] RECOVERY 마켓도 position 동기화 (추가 구매 반영)
                        _sync_ctx_position(market, ctx, qty, avg_buy, source="reconcile:recovery")
                    else:
                        # Legacy behaviour: only recover missing position into context
                        if ctx.position is None:
                            src = "orphan" if self.oma_registry.get_state(market) != MarketState.ACTIVE else "reconcile:missing"
                            _sync_ctx_position(market, ctx, qty, avg_buy, source=src)

                    # Orphan detection: holding exists but market not ACTIVE
                    current_state = self.oma_registry.get_state(market)
                    if current_state not in (MarketState.ACTIVE, MarketState.RECOVERY):
                        # [2026-03-08] 쿨다운 중인 코인은 orphan 승격 스킵
                        # 매도 직후 잔고가 남아있어도 RECOVERY로 잡아넣지 않음
                        try:
                            _cd_map = getattr(self, "autopilot_cooldown", {}) or {}
                            _cd_entry = _cd_map.get(market)
                            if _cd_entry:
                                _cd_until = float(_cd_entry.get("until_ts") or 0.0) if isinstance(_cd_entry, dict) else float(_cd_entry or 0.0)
                                if _cd_until > time.time():
                                    continue  # 쿨다운 중 — 매도 직후이므로 skip
                        except (AttributeError, TypeError, ValueError) as exc:
                            logger.error("[cooldown_check] sell cooldown check failed: %s", exc, exc_info=True)

                        # [PATCH] Dust check: do not promote if value < min_order_usdt
                        # This prevents infinite loop of Promote -> Fail Sell -> Demote -> Promote
                        est_price = float(info.get("price") or 0.0)
                        if (not est_price or est_price <= 0):
                            est_price = price_store.get_price(market)
                        if (not est_price or est_price <= 0) and avg_buy > 0:
                            est_price = avg_buy

                        est_val = (float(est_price) * float(qty)) if est_price else 0.0

                        # If value is dust, skip promotion (leave it in WATCH/DISABLED/PURGED)
                        if est_val > 0 and est_val < self.min_order_usdt:
                            continue

                        # [FIX] 수동 구매 코인: 실제 구매 금액을 budget_usdt로 설정
                        # 이렇게 해야 예산 분배 시 이 금액을 제외하고 나머지를 분배함
                        purchase_usdt = float(avg_buy) * float(qty) if avg_buy > 0 else est_val

                        # [PROTECTED] GAZUA 전략이 이미 설정되어 있으면 ACTIVE로 승격
                        # DO NOT MODIFY: 이 로직은 사용자 지시로 보호됨 (2026-01-23)
                        is_gazua_already = False
                        try:
                            s_mode = str(((ctx.controls or {}).get("strategy") or {}).get("mode") or "").upper()
                            is_gazua_already = (s_mode == "GAZUA")
                        except (AttributeError, TypeError, ValueError) as exc:
                            logger.error("[orphan_detect] GAZUA check during orphan detect failed: %s", exc, exc_info=True)

                        # 기존 budget_usdt가 있으면 유지 (앱에서 설정한 예산)
                        existing_budget = self.oma_registry.get_budget_usdt(market)
                        final_budget = float(existing_budget) if existing_budget and float(existing_budget) > 0 else purchase_usdt

                        # GAZUA 설정됨 → ACTIVE, 아니면 → RECOVERY
                        target_state = MarketState.ACTIVE if is_gazua_already else MarketState.RECOVERY

                        self.oma_registry.set_state(
                            market=market,
                            state=target_state,
                            reason=["orphan_detected", "manual_purchase"],
                            budget_usdt=final_budget  # 기존 예산 유지 또는 구매 금액
                        )
                        ctx.market_state = target_state.value
                        ctx.recovery = (target_state == MarketState.RECOVERY)
                        ctx.recovery_reason = "orphan_detected" if ctx.recovery else None
                        if getattr(ctx, "recovery_since_ts", None) is None:
                            try:
                                ctx.recovery_since_ts = time.time()
                            except (AttributeError, TypeError, ValueError) as exc:
                                logger.error("[orphan_strategy] GAZUA strategy assignment failed: %s", exc, exc_info=True)

                        # allocated_capital도 실제 구매 금액으로 설정
                        ctx.allocated_capital = purchase_usdt
                        self.ledger.append("ORPHAN_BUDGET_SET", market=market, budget_usdt=purchase_usdt)

                        # [PATCH] 수동 구매 코인: 기존 전략이 LADDER/SNIPER면 유지, 아니면 GAZUA로 덮어씀
                        try:
                            from app.manager.market_controls import apply_engine_controls
                            s_mode_existing = str(((ctx.controls or {}).get("strategy") or {}).get("mode") or "").upper()
                            _protected = {"LADDER", "SNIPER", "SNIPERS"}
                            if s_mode_existing in _protected:
                                self.ledger.append("ORPHAN_DEFAULT_STRATEGY", market=market, strategy=s_mode_existing)
                                # Do NOT overwrite LADDER / SNIPER strategy
                            else:
                                apply_engine_controls(self, market, "GAZUA")
                                self.ledger.append("ORPHAN_DEFAULT_STRATEGY", market=market, strategy="GAZUA")
                        except (KeyError, AttributeError, TypeError, ValueError) as exc:
                            logger.error("[ORPHAN] strategy assignment FAILED for %s: %s — orphan may lack strategy",
                                         market, exc, exc_info=True)

                        promoted.append(market)
                        self.ledger.append("ORPHAN_DETECTED", market=market, currency=cur, qty=float(qty), avg_buy_price=float(avg_buy))
                        orphans.append({"market": market, "currency": cur, "qty": float(qty), "avg_buy_price": float(avg_buy)})

                    # Already RECOVERY - check if dust → auto-purge
                    elif current_state == MarketState.RECOVERY:
                        # [2026-03-08] RECOVERY 먼지 자동 정리:
                        # min_order_usdt 미만 잔고는 매도 불가 → DISABLED로 전환
                        _rec_price = float(info.get("price") or 0.0)
                        if _rec_price <= 0:
                            _rec_price = price_store.get_price(market) or 0.0
                        _rec_val = float(_rec_price) * float(qty) if _rec_price else 0.0
                        if 0 < _rec_val < self.min_order_usdt:
                            try:
                                self.oma_registry.set_state(
                                    market=market,
                                    state=MarketState.DISABLED,
                                    reason=["recovery_dust_cleanup", f"val={int(_rec_val)}"],
                                )
                                ctx.market_state = MarketState.DISABLED.value
                                ctx.recovery = False
                                ctx._dust_disabled = True  # reconcile 재승격 차단 플래그
                                self.ledger.append("RECOVERY_DUST_PURGE", market=market, val_usdt=_rec_val)
                            except (KeyError, AttributeError, TypeError, ValueError) as exc:
                                logger.error("[dust_purge] dust position purge failed: %s", exc, exc_info=True)
                            continue

                        orphans.append({"market": market, "currency": cur, "qty": float(qty), "avg_buy_price": float(avg_buy)})

                        # [FIX] 기존 RECOVERY 코인도 budget_usdt 없으면 설정
                        existing_budget = self.oma_registry.get_budget_usdt(market)
                        if not existing_budget or existing_budget <= 0:
                            purchase_usdt = float(avg_buy) * float(qty) if avg_buy > 0 else 0.0
                            if purchase_usdt > 0:
                                self.oma_registry.set_state(
                                    market=market,
                                    state=MarketState.RECOVERY,
                                    reason=["recovery_budget_upgrade"],
                                    budget_usdt=purchase_usdt
                                )
                                ctx.allocated_capital = purchase_usdt
                                self.ledger.append("RECOVERY_BUDGET_SET", market=market, budget_usdt=purchase_usdt)

                        # [FIX] RECOVERY 코인이 GAZUA가 아니면 GAZUA로 설정
                        existing_strategy = ""
                        try:
                            existing_strategy = str(((ctx.controls or {}).get("strategy") or {}).get("mode") or "").upper()
                        except (AttributeError, TypeError, ValueError) as exc:
                            logger.error("[recovery_strategy] GAZUA strategy read failed: %s", exc, exc_info=True)
                        if existing_strategy != "GAZUA":
                            try:
                                from app.manager.market_controls import apply_engine_controls
                                apply_engine_controls(self, market, "GAZUA")
                                self.ledger.append("RECOVERY_STRATEGY_FIXED", market=market, old=existing_strategy, new="GAZUA")
                            except (AttributeError, TypeError) as exc:
                                logger.error("[recovery_strategy] GAZUA strategy apply failed: %s", exc, exc_info=True)

                    # [FIX] 모든 holding: allocated를 실제 구매금액으로 동기화 + OMA budget_usdt도 동기화
                    # [PROTECTED] GAZUA 전략은 사용자 설정 예산 우선 - 포지션 가치로 덮어쓰지 않음
                    # DO NOT MODIFY: 이 로직은 사용자 지시로 보호됨 (2026-01-23)
                    if qty > 0 and avg_buy > 0:
                        pos_value = float(qty) * float(avg_buy)

                        # GAZUA 체크
                        is_gazua = False
                        try:
                            s_mode = str(((ctx.controls or {}).get("strategy") or {}).get("mode") or "").upper()
                            is_gazua = (s_mode == "GAZUA")
                        except (AttributeError, TypeError, ValueError) as exc:
                            logger.error("[capital_sync] GAZUA check failed: %s", exc, exc_info=True)

                        existing_budget = self.oma_registry.get_budget_usdt(market)

                        # GAZUA: 기존 예산이 있으면 유지, 없으면 포지션 가치 사용
                        if is_gazua and existing_budget and float(existing_budget) > 0:
                            # GAZUA는 사용자 설정 예산 유지 (덮어쓰지 않음)
                            if ctx.allocated_capital <= 0:
                                ctx.allocated_capital = float(existing_budget)
                                self.ledger.append("ALLOCATED_SYNC", market=market, old=0, new=float(existing_budget), source="gazua_budget_preserve")
                        else:
                            # GAZUA가 아닌 전략: 사용자 설정 예산 우선
                            # [FIX 2026-01-25] registry에 예산이 있으면 사용자 설정으로 간주하고 유지
                            if existing_budget and float(existing_budget) > 0:
                                # 사용자가 수동으로 예산을 설정한 경우 → 유지
                                if ctx.allocated_capital <= 0:
                                    ctx.allocated_capital = float(existing_budget)
                                    self.ledger.append("ALLOCATED_SYNC", market=market, old=0, new=float(existing_budget), source="user_budget_preserve")
                                # 이미 allocated가 있으면 유지 (덮어쓰지 않음)
                            elif pos_value > 0 and ctx.allocated_capital <= 0:
                                # 예산 설정 없고 포지션만 있으면 포지션 가치로 복구
                                ctx.allocated_capital = pos_value
                                self.ledger.append("ALLOCATED_SYNC", market=market, old=0, new=pos_value, source="pos_fallback")

                                # OMA budget_usdt도 동기화 (신규 발견 코인만)
                                self.oma_registry.set_state(
                                    market=market,
                                    state=current_state,
                                    reason=["budget_sync"],
                                    budget_usdt=pos_value
                                )
                                self.ledger.append("BUDGET_SYNC", market=market, old=existing_budget, new=pos_value)

                except Exception as exc:
                    logger.error(
                        "[RECONCILE] position sync FAILED for market, skipping: %s",
                        exc, exc_info=True,
                    )
                    continue

            # Clear positions for synced/manual markets with no holdings (manual sell / external close)
            # NOTE: "should_clear" is more permissive than "should_manage" (full sync).
            # Clearing a zero-qty position is low-risk and should work even when sync_mode=OFF.
            for market, ctx in self.coordinator.get_contexts().items():
                try:
                    if self._known_markets and market not in self._known_markets:
                        continue
                    cur = market.split("-", 1)[1] if "-" in market else ""
                    if cur and cur.upper() in skip_ccy:
                        continue

                    # Skip if there's an active order (let OSM handle it)
                    if getattr(ctx, "order_state", None):
                        continue

                    manual_ctl = (ctx.controls or {}).get("manual") or {}
                    manual_enabled = bool(manual_ctl.get("enabled"))

                    # Determine strategy mode
                    s_mode = ""
                    try:
                        s_mode = str(((ctx.controls or {}).get("strategy") or {}).get("mode") or "").upper()
                    except (KeyError, AttributeError, TypeError, ValueError):
                        logger.warning("[reconcile] strategy mode parse error during position sync", exc_info=True)
                        s_mode = ""

                    should_clear = False
                    source = ""

                    # 1) Manual mode: always manage
                    if manual_enabled:
                        should_clear = True
                        source = "manual"
                    # 2) sync_mode == ALL: always manage
                    elif sync_mode == "ALL":
                        should_clear = True
                        source = "reconcile:all"
                    # 3) sync_mode == ACTIVE: manage ACTIVE and RECOVERY states
                    elif sync_mode == "ACTIVE" and self.oma_registry.get_state(market) in (MarketState.ACTIVE, MarketState.RECOVERY):
                        should_clear = True
                        source = "reconcile:active"
                    # 4) GAZUA strategy: always allow clearing (external buy/sell is the norm)
                    elif s_mode == "GAZUA":
                        should_clear = True
                        source = "reconcile:gazua_clear"
                    # 5) Any context with existing position in ACTIVE/RECOVERY: allow clearing
                    #    This catches positions that lost their strategy tag after restart
                    elif ctx.position is not None and self.oma_registry.get_state(market) in (MarketState.ACTIVE, MarketState.RECOVERY):
                        should_clear = True
                        source = "reconcile:position_clear"

                    if not should_clear:
                        continue

                    if market not in holdings:
                        _sync_ctx_position(market, ctx, 0.0, 0.0, source=source)
                except (KeyError, IndexError, AttributeError, TypeError, ValueError) as exc:
                    logger.error("[RECONCILE] position sync FAILED for %s: %s — position may be stale",
                                 market, exc, exc_info=True)
                    continue
        # 새로 RECOVERY가 생겼으면 pricefeed 재구독
        if promoted:
            for mk in sorted(set(promoted)):
                try:
                    self.coordinator.activate_market(mk)
                except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
                    logger.error("[RECONCILE] activate_market FAILED for %s: %s", mk, exc, exc_info=True)
            try:
                self.price_feed.request_resubscribe()
            except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
                logger.error("[RECONCILE] price_feed resubscribe FAILED after orphan promotion: %s", exc, exc_info=True)

        # RECOVERY 자동 청산(정책 AUTO)
        if self.order_fsm and self.recovery_policy == "AUTO":
            for o in orphans:
                mk = o["market"]
                ctx = self.coordinator.ensure_market(mk)
                if not ctx.position or ctx.order_state:
                    continue
                qty = float(ctx.position.get("qty") or 0.0)
                if qty <= 0:
                    continue
                expected = price_store.get_price(mk)
                # 최소 가치 가드
                if expected and (qty * float(expected)) < float(self.recovery_min_value_usdt):
                    self.ledger.append("RECOVERY_SKIP_MIN_VALUE", market=mk, qty=qty, expected_price=expected)
                    continue
                self.order_fsm.submit_market_sell(ctx=ctx, market=mk, qty=qty, expected_price=expected, reason="recovery:auto")

        # [PATCH] Auto-retire completed markets (all strategies)
        # 포지션이 청산되면(잔고 0) 자동으로 DISABLED 상태로 퇴장시킵니다.
        # 환경변수로 비활성화 가능: OMA_AUTO_RETIRE_EMPTY=false
        auto_retire_enabled = _env_bool("OMA_AUTO_RETIRE_EMPTY", True)

        if auto_retire_enabled:
            for market in list(self.oma_registry.list_active()):
                try:
                    ctx = self.coordinator.contexts.get(market)
                    if not ctx: continue

                    # Check if position is empty (ctx.position)
                    pos = getattr(ctx, "position", None)
                    ctx_qty = float(pos.get("qty") or 0.0) if pos else 0.0
                    if ctx_qty > 0:
                        continue

                    # Double-check: Bybit 실잔고도 확인 (안전장치)
                    # holdings는 reconcile 시작 시 조회한 Bybit 잔고
                    real_qty = 0.0
                    if market in holdings:
                        real_qty = float(holdings[market].get("qty") or 0.0)
                    if real_qty > 0:
                        # 실제 잔고가 있으면 퇴장하지 않음
                        continue

                    # Triple-check: Bybit accounts API로 직접 한 번 더 확인
                    # 단, min_order 미만 먼지(dust) 잔액은 퇴장 허용
                    dust_threshold_usdt = float(getattr(self, 'min_order_usdt', Q.min_order) or Q.min_order)
                    try:
                        base_cur = Q.extract_base(market)
                        for a in accounts:
                            if str(a.get("currency") or "").upper() == base_cur:
                                acct_qty = float(a.get("balance") or 0.0) + float(a.get("locked") or 0.0)
                                if acct_qty > 0:
                                    # 가치 계산
                                    price = price_store.get_price(market) or float(a.get("avg_buy_price") or 0)
                                    value_usdt = acct_qty * price if price else 0
                                    if value_usdt > dust_threshold_usdt:
                                        # 의미있는 잔액 - 퇴장하지 않음
                                        real_qty = acct_qty
                                    break
                    except (KeyError, AttributeError, TypeError, ValueError) as exc:
                        logger.error("[retirement] balance check during retirement failed: %s", exc, exc_info=True)
                    if real_qty > 0:
                        continue

                    # Check if we should retire
                    # 1. Just cleared in this reconcile (Manual Sell in App)
                    is_cleared = (market in cleared)

                    # 2. Exited via Engine (Auto Sell) since activation
                    last_exit = float(getattr(ctx, "last_exit_ts", 0.0) or 0.0)
                    started = float(getattr(ctx, "engine_started_ts", 0.0) or 0.0)
                    is_auto_exited = (last_exit > started)

                    if is_cleared or is_auto_exited:
                        strategy = str(((ctx.controls or {}).get("strategy") or {}).get("mode") or "unknown").upper()

                        # 원장에 퇴장 기록
                        self.ledger.append(
                            "MARKET_RETIRED",
                            market=market,
                            strategy=strategy,
                            reason="position_cleared",
                        )

                        # DISABLED로 설정 (리스트에서 완전 제거)
                        self.oma_set_market(
                            market=market,
                            state=MarketState.DISABLED,
                            reason=[f"{strategy.lower()}_completed"]
                        )

                        # 컨텍스트에서도 제거
                        self.coordinator.remove_market(market)
                except (KeyError, AttributeError, TypeError, ValueError) as exc:
                    logger.error("[retirement] context removal during retirement failed: %s", exc, exc_info=True)

        # [2026-03-08] RECOVERY 좀비 자동 정리:
        # 잔고가 없는 RECOVERY 코인은 DISABLED로 전환 (먼지청소기가 Bybit만 정리하고 OMA는 안 건드려서 좀비가 됨)
        if auto_retire_enabled:
            try:
                for market in list(self.oma_registry.list_recovery()):
                    try:
                        # Bybit 잔고 확인
                        if market in holdings:
                            _hq = float(holdings[market].get("qty") or 0.0)
                            if _hq > 0:
                                continue  # 실제 잔고 있음 — 유지

                        # holdings에 없음 = 잔고 0 (또는 먼지)
                        # accounts 원본에서도 재확인
                        _has_real_balance = False
                        try:
                            _base_cur = Q.extract_base(market)
                            for a in accounts:
                                if str(a.get("currency") or "").upper() == _base_cur:
                                    _aq = float(a.get("balance") or 0) + float(a.get("locked") or 0)
                                    if _aq > 0:
                                        _ap = price_store.get_price(market) or float(a.get("avg_buy_price") or 0)
                                        if _ap and (_aq * float(_ap)) >= self.min_order_usdt:
                                            _has_real_balance = True
                                    break
                        except (KeyError, AttributeError, TypeError, ValueError) as exc:
                            logger.error("[retirement] fallback account balance check failed: %s", exc, exc_info=True)
                        if _has_real_balance:
                            continue

                        # 잔고 없거나 먼지 → DISABLED
                        self.oma_set_market(
                            market=market,
                            state=MarketState.DISABLED,
                            reason=["recovery_zombie_cleanup"],
                        )
                        try:
                            self.coordinator.remove_market(market)
                        except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
                            logger.error("[zombie_cleanup] context removal for DISABLED market failed: %s", exc, exc_info=True)
                        self.ledger.append("RECOVERY_ZOMBIE_PURGE", market=market)
                    except (KeyError, AttributeError, TypeError, ValueError) as exc:
                        logger.error("[zombie_cleanup] per-market DISABLED cleanup failed: %s", exc, exc_info=True)
            except (KeyError, AttributeError, TypeError, ValueError) as exc:
                logger.error("[zombie_cleanup] DISABLED cleanup iteration failed: %s", exc, exc_info=True)

        self._save_context_state()

        # 활성 마켓 중 거래지원 종료 예정 체크
        delisting_alerts = []
        try:
            from app.manager.market_status_monitor import get_active_delisting_markets
            active_set = set(self.oma_registry.list_active())
            delisting_alerts = get_active_delisting_markets(active_set)

            for alert in delisting_alerts:
                mkt = alert["market"]
                ddate = alert.get("delisting_date") or "미정"
                kname = alert.get("korean_name") or mkt

                # 원장 기록
                self.ledger.append(
                    "DELISTING_WARNING",
                    level="WARN",
                    market=mkt,
                    korean_name=kname,
                    delisting_date=ddate,
                )

                # 텔레그램 알림 (1회만)
                try:
                    from app.notify.telegram import send_telegram
                    cache_key = f"_delisting_notified_{mkt}"
                    if not getattr(self, cache_key, False):
                        send_telegram(f"⚠️ 거래지원 종료 예정\n\n{kname} ({mkt})\n종료일: {ddate}\n\n보유 중인 코인입니다. 확인해주세요.")
                        setattr(self, cache_key, True)
                except (KeyError, AttributeError, TypeError, ValueError) as exc:
                    logger.warning("[telegram] delisting notification failed: %s", exc, exc_info=True)

            # 종료 예정 마켓 자동 청산 (옵션)
            from app.core.constants import env_bool
            auto_liquidate = env_bool("OMA_AUTO_LIQUIDATE_DELISTING", default=False)

            if auto_liquidate and delisting_alerts:
                for alert in delisting_alerts:
                    mkt = alert["market"]
                    kname = alert.get("korean_name") or mkt

                    liq_key = f"_delisting_liquidated_{mkt}"
                    if getattr(self, liq_key, False):
                        continue

                    try:
                        accounts = self.trade_client.accounts(skip_currencies=["USDT"])
                        cur = Q.extract_base(mkt)
                        for a in accounts:
                            if str(a.get("currency") or "").upper() == cur:
                                qty = float(a.get("balance") or 0)
                                if qty > 0:
                                    from app.integrations.bybit_trade import market_sell
                                    result_sell = market_sell(market=mkt, volume=qty)

                                    self.ledger.append(
                                        "DELISTING_AUTO_LIQUIDATE",
                                        level="WARN",
                                        market=mkt,
                                        korean_name=kname,
                                        qty=qty,
                                        result=str(result_sell)[:200],
                                    )

                                    from app.notify.telegram import send_telegram
                                    send_telegram(f"🚨 종료 예정 자동 청산\n\n{kname} ({mkt})\n수량: {qty}\n\n거래지원 종료 예정으로 자동 매도되었습니다.")

                                    setattr(self, liq_key, True)
                                break
                    except (KeyError, IndexError, AttributeError, TypeError, ValueError) as e:
                        self.ledger.append("DELISTING_LIQUIDATE_ERROR", market=mkt, error=str(e)[:200])
        except (KeyError, IndexError, AttributeError, TypeError, ValueError) as exc:
            logger.error("[delisting] delisting check fallback failed: %s", exc, exc_info=True)

        # 신규 상장 체크 및 알림
        try:
            from app.manager.market_status_monitor import check_market_status_changes
            changes = check_market_status_changes(active_markets=set(self.oma_registry.list_active()))

            for listing in changes.get("new_listings", []):
                mkt = listing["market"]
                kname = listing.get("korean_name") or mkt

                self.ledger.append(
                    "NEW_LISTING",
                    level="INFO",
                    market=mkt,
                    korean_name=kname,
                )

                try:
                    from app.notify.telegram import send_telegram
                    cache_key = f"_listing_notified_{mkt}"
                    if not getattr(self, cache_key, False):
                        send_telegram(f"🆕 신규 상장!\n\n{kname} ({mkt})\n\nBybit에 새로 상장되었습니다.")
                        setattr(self, cache_key, True)
                except (KeyError, AttributeError, TypeError, ValueError) as exc:
                    logger.warning("[new_listing] per-market new listing check failed: %s", exc, exc_info=True)
        except (KeyError, AttributeError, TypeError, ValueError) as exc:
            logger.warning("[new_listing] new listing check failed: %s", exc, exc_info=True)

        # ★ FOCUS 포지션 Bybit 실잔고 검증 (reconcile마다)
        try:
            fm = getattr(self, "focus_manager", None)
            if fm and getattr(fm, "enabled", False) and fm.positions:
                fm._sync_with_bybit()
        except Exception as exc:
            logger.debug("[Reconcile] FOCUS sync: %s", exc)

        result = {
            "ok": True,
            "mode": self.trading_mode,
            "reason": reason,
            "position_sync_mode": sync_mode,
            "position_sync": {"synced": synced, "cleared": cleared},
            "orphans": orphans,
            "delisting_alerts": delisting_alerts,
            "equity": {
                "cash_usdt": cash_usdt,
                "deployed_usdt": deployed_usdt,
                "equity_usdt": equity_usdt,
                "ts": self._last_equity_ts,
            },
        }
        # [2026-03-09] 변화 있을 때만 원장 기록 (orphan/sync/clear 발생 시)
        _has_reconcile_change = (len(orphans) > 0 or len(synced) > 0 or len(cleared) > 0)
        _reconcile_elapsed = time.time() - getattr(self, '_last_reconcile_log_ts', 0.0)
        if _has_reconcile_change or _reconcile_elapsed >= 300.0:
            self.ledger.append("RECONCILE_OK", reason=reason, orphans=len(orphans), synced=len(synced), cleared=len(cleared), sync_mode=sync_mode)
            self._last_reconcile_log_ts = time.time()
        return result

    # --------------------------------------------------------
    # Emergency control
    # --------------------------------------------------------
    def set_emergency_stop(self, enabled: bool, *, reason: str = "") -> None:
        self.emergency_stop = bool(enabled)
        self.ledger.append("EMERGENCY_STOP_SET", enabled=bool(enabled), reason=reason)

    # --------------------------------------------------------
    # Recovery manual hook
    # --------------------------------------------------------
    def request_recovery_liquidate(self, *, market: str, reason: str = "manual") -> Dict[str, Any]:
        """RECOVERY(회수 모드) 시장을 수동으로 전량 청산 요청.

        - LIVE에서만 의미가 있고, order_fsm이 필요하다.
        - 진입은 금지, 청산만 허용.
        """
        if not self.order_fsm:
            return {"ok": False, "error": "order_fsm_not_ready", "mode": self.trading_mode}

        mk = str(market)
        ctx = self.coordinator.ensure_market(mk)
        ctx.market_state = self.oma_registry.get_state(mk).value
        ctx.trading_mode = self.trading_mode

        # 보유 없으면 reconcile을 한번 유도할 수 있지만, 여기서는 즉시 실패
        if not ctx.position:
            return {"ok": False, "error": "no_position", "market": mk}

        if ctx.order_state:
            return {"ok": False, "error": "order_pending", "market": mk}

        qty = float(ctx.position.get("qty") or 0.0)
        if qty <= 0:
            return {"ok": False, "error": "qty<=0", "market": mk}

        expected = price_store.get_price(mk)
        ok, msg = self.order_fsm.submit_market_sell(ctx=ctx, market=mk, qty=qty, expected_price=expected, reason=f"recovery:{reason}")
        return {"ok": bool(ok), "market": mk, "qty": qty, "uuid": str(msg) if ok else None, "error": None if ok else str(msg)}

    # --------------------------------------------------------
    # Recovery policy tick
    # --------------------------------------------------------
    async def _maybe_apply_recovery_policy(self, *, market: str, price: float, ctx: Any) -> None:
        if not self.order_fsm:
            return
        if not getattr(ctx, "recovery", False):
            return
        if getattr(ctx, "order_state", None):
            return
        if not getattr(ctx, "position", None):
            return

        qty = float(ctx.position.get("qty") or 0.0)
        if qty <= 0:
            return

        # 최소 가치 가드
        try:
            if float(price) * float(qty) < float(self.recovery_min_value_usdt):
                return
        except (TypeError, ValueError) as exc:
            try:
                self.ledger.append("RECOVERY_POLICY_ERROR", market=market, where="min_value_guard", error=str(exc))
            except (AttributeError, TypeError, ValueError) as exc:
                logger.warning("[recovery] min value guard ledger append failed: %s", exc, exc_info=True)
            return

        pol = self.recovery_policy
        if pol == "HOLD":
            return

        now = time.time()
        if getattr(ctx, "recovery_since_ts", None) is None:
            try:
                ctx.recovery_since_ts = now
            except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
                logger.warning("[recovery] min value guard recovery_since_ts set failed: %s", exc, exc_info=True)

        if pol == "AUTO":
            await self._handle_intent(market=market, price=float(price), ctx=ctx, intent={"action": "sell", "qty": qty, "reason": "recovery:auto"})
            return

        if pol == "CONDITIONAL":
            # held time
            try:
                since = float(getattr(ctx, "recovery_since_ts") or now)
            except (TypeError, ValueError):
                logger.warning("[recovery] recovery_since_ts parse error for %s", market, exc_info=True)
                since = now

            held = max(0.0, now - since)

            # pnl
            entry = 0.0
            try:
                entry = float(ctx.position.get("entry") or 0.0)
            except (AttributeError, TypeError, ValueError):
                logger.warning("[recovery] entry price parse error for %s", market, exc_info=True)
                entry = 0.0

            pnl_pct = None
            if entry and entry > 0:
                pnl_pct = (float(price) - entry) / entry * 100.0

            trigger = None
            if held >= float(self.recovery_cond_max_hold_sec):
                trigger = f"held>={self.recovery_cond_max_hold_sec}s"
            elif pnl_pct is not None and pnl_pct <= -float(self.recovery_cond_stoploss_pct):
                trigger = f"pnl<={-self.recovery_cond_stoploss_pct}%"

            if trigger:
                self.ledger.append("RECOVERY_COND_TRIGGER", market=market, trigger=trigger, pnl_pct=pnl_pct, held_sec=held)
                await self._handle_intent(market=market, price=float(price), ctx=ctx, intent={"action": "sell", "qty": qty, "reason": f"recovery:cond:{trigger}"})

    # --------------------------------------------------------
    # Dust Cleanup
    # --------------------------------------------------------
    async def _check_auto_dust_vacuum(self) -> None:
        """[2026-02-01] 자동 먼지 청소 체크 및 실행."""
        try:
            if not bool(getattr(self, "dust_vacuum_enabled", False)):
                return

            import datetime
            today = datetime.date.today().isoformat()  # YYYY-MM-DD

            # 날짜가 바뀌면 카운터 리셋
            if getattr(self, "dust_vacuum_last_run_date", "") != today:
                self.dust_vacuum_last_run_date = today
                self.dust_vacuum_today_count = 0

            daily_limit = int(getattr(self, "dust_vacuum_daily_count", 1) or 1)
            if self.dust_vacuum_today_count >= daily_limit:
                return  # 오늘 할당량 소진

            # 실행 (하루에 N회, 간격을 두고)
            # 예: 하루 2회면 12시간 간격, 1회면 24시간 중 아무때나
            threshold_usdt = float(getattr(self, "dust_vacuum_threshold_usdt", 5.0) or 5.0)

            result = await self._run_dust_vacuum(threshold_usdt=threshold_usdt)

            if result.get("vacuumed_count", 0) > 0:
                self.dust_vacuum_today_count += 1
                self.ledger.append(
                    "AUTO_DUST_VACUUM",
                    date=today,
                    count=self.dust_vacuum_today_count,
                    vacuumed=result.get("vacuumed_count", 0),
                    results=result.get("results", [])[:5]  # 로그 크기 제한
                )

                # 텔레그램 알림
                try:
                    import asyncio
                    from app.notify.telegram import send_telegram
                    await asyncio.to_thread(send_telegram,
                        f"🧹 *자동 먼지 청소 완료*\n\n"
                        f"📅 {today}\n"
                        f"🪙 {result.get('vacuumed_count', 0)}개 코인 청소\n"
                        f"📊 오늘 {self.dust_vacuum_today_count}/{daily_limit}회"
                    )
                except (KeyError, AttributeError, TypeError, ValueError) as exc2:
                    logger.warning("[telegram] dust vacuum notification failed: %s", exc2, exc_info=True)

        except (OSError, KeyError, IndexError, AttributeError, TypeError, ValueError, OverflowError) as exc:
            try:
                self.ledger.append("AUTO_DUST_VACUUM_ERROR", error=str(exc))
            except (AttributeError, TypeError, ValueError) as exc2:
                logger.warning("[ledger] dust vacuum error ledger append failed: %s", exc2, exc_info=True)

    async def _run_dust_vacuum(self, threshold_usdt: float = 5.0) -> Dict[str, Any]:
        """실제 먼지 청소 실행."""
        import asyncio
        from app.core.currency import Q
        from app.core.hyper_price_store import price_store

        if not self.trade_client:
            return {"ok": False, "error": "NO_TRADE_CLIENT", "vacuumed_count": 0}

        try:
            accounts = self.trade_client.accounts()
        except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as e:
            logger.warning("[dust_vacuum] failed to fetch accounts", exc_info=True)
            return {"ok": False, "error": str(e), "vacuumed_count": 0}

        min_order_usdt = Q.min_order
        results = []

        for acc in accounts:
            try:
                cur = str(acc.get("currency") or "").upper()
                if cur == Q.symbol:  # USDT (quote currency)
                    continue

                balance = float(acc.get("balance") or 0)
                if balance <= 0:
                    continue

                market = Q.market(cur)
                price = price_store.get_price(market)
                if not price:
                    price = float(acc.get("avg_buy_price") or 0)
                if not price or price <= 0:
                    continue

                value_usdt = balance * price
                if value_usdt >= threshold_usdt:
                    continue  # 먼지 아님

                # 먼지 청소 실행
                need_buy = value_usdt < min_order_usdt
                buy_result = None
                sell_result = None

                if need_buy:
                    # 최소 금액 매수
                    try:
                        buy_result = self.trade_client.market_buy(market, min_order_usdt)
                        await asyncio.sleep(3)  # 체결 대기
                        balance = self.trade_client.get_balance(cur, include_locked=False)
                    except Exception as e:
                        logger.error("[dust_vacuum] %s BUY_FAILED: %s", market, e, exc_info=True)
                        results.append({"market": market, "error": f"BUY_FAILED: {e}"})
                        continue

                # 전량 매도
                if balance > 0:
                    try:
                        sell_result = self.trade_client.market_sell(market, balance)
                        results.append({
                            "market": market,
                            "ok": True,
                            "bought": need_buy,
                            "sold_qty": balance,
                        })
                    except (AttributeError, TypeError) as e:
                        results.append({"market": market, "error": f"SELL_FAILED: {e}"})

                await asyncio.sleep(1)  # Rate limit

            except Exception as exc:
                logger.error("[dust_vacuum] sell failed: %s", exc, exc_info=True)
                continue

        return {
            "ok": True,
            "vacuumed_count": len([r for r in results if r.get("ok")]),
            "results": results,
        }

    def cleanup_dust_markets(self, threshold_usdt: float = 0.0, purge: bool = False) -> Dict[str, Any]:
        """
        평가금액이 threshold_usdt(기본값: min_order_usdt) 미만인 ACTIVE/RECOVERY 마켓을 정리한다.
        purge=True이면 아예 시스템에서 삭제(Purge)하여 대시보드에서 안 보이게 한다.
        """
        if threshold_usdt <= 0:
            threshold_usdt = self.min_order_usdt

        cleaned = []
        # ACTIVE + RECOVERY 대상
        active = self.oma_registry.list_active()
        recovery = []
        try:
            recovery = self.oma_registry.list_recovery()
        except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
            logger.error("[recovery] ACTIVE+RECOVERY list retrieval failed: %s", exc, exc_info=True)

        targets = set(active + recovery)

        for market in targets:
            ctx = self.coordinator.get_context(market)
            if not ctx or not ctx.position:
                continue

            qty = float(ctx.position.get("qty") or 0.0)
            if qty <= 0:
                continue

            # Value estimation
            price = price_store.get_price(market)
            if not price:
                price = float(ctx.position.get("entry") or 0.0)

            val = qty * float(price or 0.0)

            # Dust condition
            if 0 < val < threshold_usdt:
                if purge:
                    self.purge_market(market, reason=f"dust_purge: {val:.0f}<{threshold_usdt:.0f}")
                else:
                    self.oma_set_market(
                        market=market,
                        state=MarketState.WATCH,
                        reason=[f"dust_cleanup: {val:.0f}<{threshold_usdt:.0f}"]
                    )

                cleaned.append({
                    "market": market,
                    "value": val,
                    "qty": qty,
                    "action": "PURGE" if purge else "WATCH"
                })
                self.ledger.append("DUST_CLEANUP", market=market, value=val, threshold=threshold_usdt, action="PURGE" if purge else "WATCH")

        return {"ok": True, "cleaned": cleaned, "count": len(cleaned), "threshold": threshold_usdt, "purge": purge}

    # --------------------------------------------------------
    # Dust Cleanup Sequence (Buy -> Sell)
    # --------------------------------------------------------
    async def _run_dust_cleanup(self, ctx, market: str, current_price: float) -> None:
        """
        먼지(소액) 잔고 청산 시퀀스:
        1. 최소주문금액(min_order_usdt)만큼 시장가 매수
        2. 체결 대기 (최대 30초)
        3. 합산된 전량 시장가 매도
        """
        # 1. Buy (minimum amount)
        # 사용자가 "최소금액"을 요청했으므로 min_order_usdt를 그대로 사용합니다.
        buy_usdt = self.min_order_usdt

        # Wallet check
        if self.wallet_mode:
             avail = float(getattr(ctx, "usable_capital", 0.0) or 0.0)
             if avail < buy_usdt:
                 self.ledger.append("DUST_CLEANUP_FAIL", market=market, reason="insufficient_usable_capital", required=buy_usdt, available=avail)
                 return

        self.ledger.append("DUST_CLEANUP_START", market=market, step="buy", amount=buy_usdt)

        ok, oid = self.order_fsm.submit_market_buy(
            ctx=ctx,
            market=market,
            usdt_amount=buy_usdt,
            expected_price=current_price,
            reason="dust_cleanup_buy",
            max_retries=3
        )

        if not ok:
            self.ledger.append("DUST_CLEANUP_FAIL", market=market, step="buy_submit", error=oid)
            return

        # 2. Wait for Buy Fill (Manual Poll)
        # Note: We block this market's task, but other markets run in parallel.
        for _ in range(30):
            await asyncio.sleep(1.0)
            try:
                order = self.trade_client.get_order(uuid=str(oid))
                state = str(order.get("state") or "")
                if state == "done":
                    # Clear pending state manually to allow next order
                    ctx.order_state = None
                    break
                if state == "cancel":
                    ctx.order_state = None
                    self.ledger.append("DUST_CLEANUP_FAIL", market=market, step="buy_canceled")
                    return
            except (KeyError, AttributeError, TypeError) as exc:
                logger.error("[position_clear] clear pending state failed: %s", exc, exc_info=True)
        else:
            # Timeout
            self.trade_client.cancel_order(uuid=str(oid))
            ctx.order_state = None
            self.ledger.append("DUST_CLEANUP_FAIL", market=market, step="buy_timeout")
            return

        # 3. Sell All
        # Fetch actual balance to be precise
        try:
            currency = market.split("-")[1]
            bal = self.trade_client.get_balance(currency)
            if bal <= 0:
                 self.ledger.append("DUST_CLEANUP_FAIL", market=market, step="sell_check", error="balance_zero")
                 return

            self.ledger.append("DUST_CLEANUP_STEP", market=market, step="sell", qty=bal)
            ok_s, oid_s = self.order_fsm.submit_market_sell(
                ctx=ctx,
                market=market,
                qty=bal,
                expected_price=current_price,
                reason="dust_cleanup_sell",
                max_retries=3
            )

            if ok_s:
                self.ledger.append("DUST_CLEANUP_SUCCESS", market=market, sell_uuid=oid_s)
            else:
                self.ledger.append("DUST_CLEANUP_FAIL", market=market, step="sell_submit", error=oid_s)

        except Exception as e:
            self.ledger.append("DUST_CLEANUP_FAIL", market=market, step="sell_exception", error=str(e))
