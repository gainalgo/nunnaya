# ============================================================
# File: app/core/hyper_system.py
# Autocoin OS v3-H — HyperSystem (LIVE order FSM + orphan recovery)
# ------------------------------------------------------------
# - OrderStateMachine 기반 LIVE 주문 통제
# - Orphan holding → WATCH가 아니라 RECOVERY(회수 관리)로 승격
# - JSONL 원장으로 가시성/복구력 강화
# - Context state(가격 tail 포함) 복원으로 리셋 후 워밍업 재시작 최소화
# ============================================================

from __future__ import annotations

import asyncio
import concurrent.futures
import functools
import json
import logging
import threading
import os
import time

logger = logging.getLogger(__name__)
from typing import Dict, Any, Optional, Set, List, Tuple

# Engine
from app.engine.hyper_engine_registry import engine_registry
from app.engine.hyper_engine_coordinator import HyperEngineCoordinator
from app.engine.hyper_engine_context import HyperEngineContext
from app.engine.hyper_nunnaya_engine import HyperNunnayaEngine


# Price
from app.core.hyper_price_store import price_store, orderbook_store
from app.core.hyper_price_feed_bybit import BybitHyperPriceFeed

# OMA
from app.manager.oma_market_registry import oma_market_registry, MarketState

# Ledger / FSM
from app.manager.trade_ledger import TradeLedger

# Portfolio Risk Manager
from app.manager.portfolio_risk_manager import get_portfolio_risk_manager

# Smart Alerts
from app.notify.smart_alerts import get_smart_alert_manager

# Integrations
from app.integrations.bybit_markets import (
    fetch_bybit_markets,
    filter_quote_markets,
    ensure_bybit_markets_cache,
)

from app.core.constants import env_bool as _env_bool, env_float as _env_float, env_int as _env_int, env_json_dict as _env_json_dict, DEFAULT_REQUEST_TIMEOUT_SEC
from app.core.runtime_paths import RuntimePaths, get_runtime_paths
from app.core.currency import Q
from app.core.bybit_trading import get_v5_order_category

# Phase 5 Mixins
from app.core.hs_mixin_state_io import StateIOMixin
from app.core.hs_mixin_ui_settings import UISettingsMixin
from app.core.hs_mixin_reconcile import ReconcileMixin
from app.core.hs_mixin_guards import GuardsMixin
from app.core.hs_mixin_budget import BudgetMixin
from app.core.hs_mixin_bg_loops import BackgroundLoopsMixin
from app.core.hs_mixin_btc_guard import BtcGuardMixin
from app.core.hs_mixin_intent import IntentMixin


def _env_csv_upper(key: str) -> List[str]:
    raw = os.getenv(key, "").strip()
    if not raw:
        return []
    return [x.strip().upper() for x in raw.split(",") if x.strip()]

class HyperSystem(StateIOMixin, UISettingsMixin, ReconcileMixin, GuardsMixin, BudgetMixin, BackgroundLoopsMixin, BtcGuardMixin, IntentMixin):
    """Autocoin OS v3-H 중앙 시스템."""

    ENGINE_NAME = "nunnaya"
    EXCHANGE_TYPE = "bybit"  # Default exchange

    def __init__(
        self,
        *,
        exchange_type: Optional[str] = None,
        runtime_paths: Optional[RuntimePaths] = None,
    ):
        # -----------------------------
        # Boot timestamp (PnL baseline fallback)
        # -----------------------------
        self._boot_ts = time.time()

        # -----------------------------
        # Exchange Configuration
        # -----------------------------
        self.exchange_type = exchange_type or self.EXCHANGE_TYPE
        self.bybit_v5_category = get_v5_order_category()

        # Runtime paths (exchange-namespaced for multi-exchange support)
        if runtime_paths is None:
            # Use legacy paths for backwards compatibility
            # Set exchange=None to use old paths like "runtime/trade_ledger.jsonl"
            self.runtime_paths = get_runtime_paths(exchange=None)
        else:
            self.runtime_paths = runtime_paths
        
        # -----------------------------
        # Engine (single)
        # -----------------------------
        self._lock = threading.RLock()
        engine = engine_registry.get(self.ENGINE_NAME)
        if engine is None:
            engine = HyperNunnayaEngine()
            engine_registry.register(self.ENGINE_NAME, engine)
        self.engine = engine

        # -----------------------------
        # Coordinator
        # -----------------------------
        self.coordinator = HyperEngineCoordinator(self.engine)

        # -----------------------------
        # OMA Registry
        # -----------------------------
        self.oma_registry = oma_market_registry
        self.oma = self.oma_registry  # API 호환

        # -----------------------------
        # Price Feed
        # -----------------------------
        self.price_feed = BybitHyperPriceFeed()

        # -----------------------------
        # Runtime mode
        # -----------------------------
        self.trading_mode = str(os.getenv("TRADING_MODE", "PAPER")).upper()
        self.strict_live = _env_bool("STRICT_LIVE", False)

        # -----------------------------
        # Safety switches
        # -----------------------------
        self.emergency_stop = _env_bool("EMERGENCY_STOP", False)

        # -----------------------------
        # Ledger
        # -----------------------------
        # Use RuntimePaths, fallback to env var for backwards compatibility
        ledger_path = os.getenv("OMA_LEDGER_PATH") or self.runtime_paths.ledger
        self.ledger = TradeLedger(path=ledger_path)

        # Tick performance log (dedicated file — 매 틱 상세 성능 기록)
        self._tick_perf_enabled = _env_bool("OMA_TICK_PERF_LOG", True)
        if self._tick_perf_enabled:
            _perf_dir = os.path.dirname(ledger_path) or "."
            self._perf_ledger = TradeLedger(
                path=os.path.join(_perf_dir, "tick_perf.jsonl"),
                max_bytes=5 * 1024 * 1024,   # 5 MB per file
                keep=5,                        # keep 5 backups (총 ~30 MB)
                run_id=self.ledger.run_id,
            )
        else:
            self._perf_ledger = None

        # -----------------------------
        # State persistence
        # -----------------------------
        # Use RuntimePaths, fallback to env var for backwards compatibility
        self.context_state_path = os.getenv("OMA_CONTEXT_STATE_PATH") or self.runtime_paths.context_state
        self.context_state_max_prices = _env_int("OMA_CONTEXT_STATE_MAX_PRICES", 500)
        self.context_state_stale_reset_sec = _env_int("OMA_CONTEXT_STATE_STALE_RESET_SEC", 900)  # 15 min
        # [OPTIMIZATION] Save interval (throttle disk I/O) — 452MB 파일, 자주 쓰면 CPU/IO 폭증
        self.context_state_save_interval_sec = _env_float("OMA_CONTEXT_STATE_SAVE_INTERVAL_SEC", 60.0)
        self._last_context_save_ts = 0.0
        # File lock for Windows safety
        self._state_lock = threading.Lock()

        # -----------------------------
        # Reconcile
        # -----------------------------
        self.reconcile_interval_sec = _env_float("OMA_RECONCILE_INTERVAL_SEC", 30.0)
        self._last_reconcile_ts: float = 0.0
        self._last_reconcile_result: Dict[str, Any] = {}

        # Reconcile position sync mode
        # OFF: do not modify ctx.position (except when ctx.position is None)
        # ACTIVE: sync positions for ACTIVE markets
        # ALL: sync positions for all contexts
        self.reconcile_position_sync_mode = str(os.getenv("RECONCILE_POSITION_SYNC_MODE", "OFF")).strip().upper()
        if self.reconcile_position_sync_mode not in ("OFF", "ACTIVE", "ALL"):
            self.reconcile_position_sync_mode = "OFF"

        # -----------------------------
        # Capital deploy (soft gate)
        # -----------------------------
        self.deploy_ratio = _env_float("DEPLOY_RATIO", 1.0)
        self.min_order_usdt = _env_float("OMA_MIN_ORDER_USDT", Q.min_order)
        self.order_cooldown_sec = _env_float("OMA_ORDER_COOLDOWN_SEC", 0.35)

        # 전략 버킷 가중치(선택)
        self.strategy_bucket_weights = _env_json_dict("OMA_STRATEGY_BUCKET_WEIGHTS")
        
        # -----------------------------
        # Smart Allocation (AI/수익 기반 예산 분배)
        # -----------------------------
        self.smart_alloc_enabled = _env_bool("OMA_SMART_ALLOC_ENABLED", True)
        self.smart_alloc_lookback_days = _env_int("OMA_SMART_ALLOC_LOOKBACK_DAYS", 7)
        self.smart_alloc_w_profit = _env_float("OMA_SMART_ALLOC_W_PROFIT", 0.5)  # 수익률 가중치
        self.smart_alloc_w_ai = _env_float("OMA_SMART_ALLOC_W_AI", 0.3)  # AI 신뢰도 가중치
        self.smart_alloc_w_risk = _env_float("OMA_SMART_ALLOC_W_RISK", 0.2)  # 리스크 감점
        self.smart_alloc_min_mult = _env_float("OMA_SMART_ALLOC_MIN_MULT", 0.5)  # 균등 대비 최소 배수
        self.smart_alloc_max_mult = _env_float("OMA_SMART_ALLOC_MAX_MULT", 2.0)  # 균등 대비 최대 배수
        self.smart_alloc_vol_th = _env_float("OMA_SMART_ALLOC_VOL_TH", 0.05)  # 변동성 페널티 임계
        self.smart_alloc_loss_penalty = _env_float("OMA_SMART_ALLOC_LOSS_PENALTY", 0.3)  # 손실 페널티
        
        # Smart Allocation 고급 요소
        # 1) Momentum Factor
        self.smart_alloc_w_momentum = _env_float("OMA_SMART_ALLOC_W_MOMENTUM", 0.15)
        self.smart_alloc_mom_lookback = _env_int("OMA_SMART_ALLOC_MOM_LOOKBACK", 24)  # 시간 단위
        self.smart_alloc_mom_scale = _env_float("OMA_SMART_ALLOC_MOM_SCALE", 2.0)
        
        # 2) Kelly Criterion
        self.smart_alloc_w_kelly = _env_float("OMA_SMART_ALLOC_W_KELLY", 0.15)
        self.smart_alloc_kelly_frac = _env_float("OMA_SMART_ALLOC_KELLY_FRAC", 0.25)  # fractional Kelly
        self.smart_alloc_kelly_max = _env_float("OMA_SMART_ALLOC_KELLY_MAX", 0.25)  # 단일 코인 상한
        self.smart_alloc_kelly_min_trades = _env_int("OMA_SMART_ALLOC_KELLY_MIN_TRADES", 5)
        
        # 3) Liquidity Factor
        self.smart_alloc_w_liquidity = _env_float("OMA_SMART_ALLOC_W_LIQUIDITY", 0.15)
        self.smart_alloc_liq_cap_ratio = _env_float("OMA_SMART_ALLOC_LIQ_CAP_RATIO", 0.001)  # 거래대금의 0.1%
        
        # 4) Correlation Penalty
        self.smart_alloc_corr_enabled = _env_bool("OMA_SMART_ALLOC_CORR_ENABLED", True)
        self.smart_alloc_corr_lookback = _env_int("OMA_SMART_ALLOC_CORR_LOOKBACK", 48)  # 시간 단위
        self.smart_alloc_corr_th = _env_float("OMA_SMART_ALLOC_CORR_TH", 0.7)  # 상관계수 임계
        self.smart_alloc_corr_lambda = _env_float("OMA_SMART_ALLOC_CORR_LAMBDA", 1.0)
        
        # 4) Sector Balancing
        self.smart_alloc_sector_enabled = _env_bool("OMA_SMART_ALLOC_SECTOR_ENABLED", True)
        self.smart_alloc_sector_map = _env_json_dict("OMA_SECTOR_MAP_JSON")  # {"BTCUSDT":"L1",...}
        self.smart_alloc_sector_caps = _env_json_dict("OMA_SECTOR_CAPS_JSON")  # {"L1":0.35,...}
        self.smart_alloc_sector_default_cap = _env_float("OMA_SECTOR_DEFAULT_CAP", 0.4)
        
        # 섹터 맵 파일에서 로드 (환경변수가 비어있으면)
        if not self.smart_alloc_sector_map:
            self._load_sector_map_from_file()

        # -----------------------------
        # Market Regime 연계 (TP/SL 조절용)
        # -----------------------------
        self.regime_enabled = _env_bool("OMA_REGIME_ENABLED", True)
        self.regime_bull_max_mult_x = _env_float("OMA_REGIME_BULL_MAX_MULT_X", 1.25)
        self.regime_bear_max_mult_x = _env_float("OMA_REGIME_BEAR_MAX_MULT_X", 0.70)
        self.regime_volatile_corr_x = _env_float("OMA_REGIME_VOLATILE_CORR_X", 1.50)

        # -----------------------------
        # 예산 배율 전략 선택 (F&G vs Regime)
        # -----------------------------
        # 설계 결정 (2026-01-23):
        # - F&G: "역발상" 선행 지표 (공포 시 매수, 탐욕 시 경계)
        # - Regime: "추세 추종" 후행 지표 (BULL이면 공격, BEAR면 보수)
        # - 환경변수로 선택 가능:
        #   * regime  = 기존 추세 추종 (BULL→x1.25, BEAR→x0.70)
        #   * fg      = F&G 역발상 (공포→x1.30, 탐욕→x0.70)
        #   * extreme = F&G 극단값(0-25, 75-100)만, 나머지는 Regime (기본값)
        #   * hybrid  = F&G × Regime 곱연산
        self.fear_greed_enabled = _env_bool("OMA_FEAR_GREED_ENABLED", True)
        self.budget_strategy = os.getenv("OMA_BUDGET_STRATEGY", "extreme").lower().strip()

        # -----------------------------
        # Exit profit guard (reverse-margin hard-fix)
        # -----------------------------
        # 목적:
        # - "같은 가격대에서 사고 파는" 초단타 루프(0.1원~1원)에서
        #   수수료/스프레드/슬리피지로 인해 역마진이 누적되는 현상을 시스템 레벨에서 차단한다.
        #
        # 원칙:
        # - SL(손절)·RECOVERY(회수)·강제 청산 같은 '필수 EXIT'는 막지 않는다.
        # - 그 외의 SELL 신호는 "예상 NET 이익"이 최소 임계값을 넘을 때만 주문을 허용한다.
        #
        # ENV:
        #   OMA_EXIT_PROFIT_GUARD=1            # on/off
        #   OMA_FEE_RATE=0.0005               # per-side fee rate (예: 0.05% → 0.0005)
        #   OMA_EXIT_SLIPPAGE_GUARD_BPS=5    # extra safety buffer (bps)
        #   OMA_EXIT_MIN_NET_PROFIT_PCT=0.03  # net profit percent threshold
        #   OMA_EXIT_MIN_NET_PROFIT_USDT=0      # net profit absolute threshold (USDT)
        self.exit_profit_guard = _env_bool("OMA_EXIT_PROFIT_GUARD", True)
        self.exit_fee_rate = _env_float("OMA_FEE_RATE", 0.0005)
        self.exit_slippage_guard_bps = _env_float("OMA_EXIT_SLIPPAGE_GUARD_BPS", 5.0)

        # ------------------------------------------------------------
        # PATCH 2025-12-26: Pingpong TP = LIMIT EXIT (best_bid) + timeout/cancel + retry
        # ------------------------------------------------------------
        self.tp_limit_exit_enabled = _env_bool("OMA_TP_LIMIT_EXIT", True)
        self.tp_limit_timeout_sec = _env_float("OMA_TP_LIMIT_TIMEOUT_SEC", 6.0)
        self.tp_limit_max_retries = _env_int("OMA_TP_LIMIT_MAX_RETRIES", 2)

        # ------------------------------------------------------------
        # PATCH 2025-12-26: ENTRY Orderbook spread/depth guard (block ENTRY)
        # ------------------------------------------------------------
        self.entry_ob_guard_enabled = _env_bool("OMA_ENTRY_OB_GUARD", True)
        self.entry_ob_max_spread_bps = _env_float("OMA_ENTRY_OB_MAX_SPREAD_BPS", 25.0)
        self.entry_ob_depth_bps = _env_float("OMA_ENTRY_OB_DEPTH_BPS", 50.0)
        self.entry_ob_depth_factor = _env_float("OMA_ENTRY_OB_DEPTH_FACTOR", 1.10)
        self.entry_ob_stale_sec = _env_float("OMA_ENTRY_OB_STALE_SEC", 3.0)
        self.entry_ob_block_cooldown_sec = _env_float("OMA_ENTRY_OB_BLOCK_COOLDOWN_SEC", 30.0)
        self.entry_ob_block_max_cooldown_sec = _env_float("OMA_ENTRY_OB_BLOCK_MAX_COOLDOWN_SEC", 300.0)
        self._ob_block_streak: Dict[str, int] = {}

        # ------------------------------------------------------------
        # PATCH 2026-01: ENTRY LIMIT BUY (지정가 진입)
        # - best_ask: 즉시 체결 가능한 가격 (체결률 높음, 슬리피지 제한)
        # - best_bid: 유리한 가격 (체결률 낮음)
        # - 미체결 시 시장가 폴백 옵션 추가
        # ------------------------------------------------------------
        self.entry_limit_buy_enabled = _env_bool("OMA_ENTRY_LIMIT_BUY", True)
        self.entry_limit_timeout_sec = _env_float("OMA_ENTRY_LIMIT_TIMEOUT_SEC", 5.0)
        self.entry_limit_price_mode = str(os.getenv("OMA_ENTRY_LIMIT_PRICE_MODE", "best_ask") or "best_ask").strip().lower()
        self.entry_limit_cooldown_sec = _env_float("OMA_ENTRY_LIMIT_COOLDOWN_SEC", 30.0)
        self.entry_limit_market_fallback = _env_bool("OMA_ENTRY_LIMIT_MARKET_FALLBACK", True)

        # ------------------------------------------------------------
        # PATCH 2025-12-26: Per-market wallet (use only its own money)
        # - Profit extracted (no auto-increase of usable_capital)
        # - Loss reflected (usable_capital decreases)
        # ------------------------------------------------------------
        self.wallet_mode = _env_bool("OMA_WALLET_MODE", True)
        self.exit_min_net_profit_pct = _env_float("OMA_EXIT_MIN_NET_PROFIT_PCT", 0.3)
        self.exit_min_net_profit_usdt = _env_float("OMA_EXIT_MIN_NET_PROFIT_USDT", 0.0)

        # -----------------------------
        # Exit profit-guard streak (consecutive block) safety
        # -----------------------------
        # 목적:
        # - profit_guard가 반복적으로 SELL을 차단하면(=시장 스프레드/수수료 대비 이익이 부족)
        #   엔진이 매 tick마다 SELL을 시도하며 로그/주문 루프가 생길 수 있다.
        # - "N회 연속 차단"이 누적되면 더 긴 쿨다운을 걸어 엔진을 진정시키고,
        #   (선택) 시장을 RECOVERY로 승격하여 운영자가 확인할 시간을 만든다.
        #
        # ENV:
        #   OMA_EXIT_PROFIT_GUARD_STREAK_N=12
        #   OMA_EXIT_PROFIT_GUARD_STREAK_WINDOW_SEC=120
        #   OMA_EXIT_PROFIT_GUARD_STREAK_COOLDOWN_SEC=60
        #   OMA_EXIT_PROFIT_GUARD_STREAK_TO_RECOVERY=0
        #   OMA_EXIT_PROFIT_GUARD_STREAK_NOTIFY=0
        self.exit_profit_guard_streak_n = _env_int("OMA_EXIT_PROFIT_GUARD_STREAK_N", 12)
        self.exit_profit_guard_streak_window_sec = _env_float("OMA_EXIT_PROFIT_GUARD_STREAK_WINDOW_SEC", 120.0)
        self.exit_profit_guard_streak_cooldown_sec = _env_float("OMA_EXIT_PROFIT_GUARD_STREAK_COOLDOWN_SEC", 60.0)
        self.exit_profit_guard_streak_to_recovery = _env_bool("OMA_EXIT_PROFIT_GUARD_STREAK_TO_RECOVERY", False)
        self.exit_profit_guard_streak_notify = _env_bool("OMA_EXIT_PROFIT_GUARD_STREAK_NOTIFY", False)

        # -----------------------------
        # Global drawdown guard (sleep-well safety)
        # -----------------------------
        # 목적:
        # - 계정 전체 equity(현금+보유자산 평가액) 기준으로 손실이 X%를 넘으면
        #   자동으로 신규 진입을 차단(쿨다운)하거나, RECOVERY/EMERGENCY_STOP으로 전환한다.
        #
        # 설계 원칙:
        # - SELL(청산)은 계속 허용해야 한다. (EMERGENCY_STOP도 BUY만 차단)
        # - RECOVERY는 "진입 금지 + 회수 허용"을 의미하며, 시스템이 강제로 청산하진 않는다.
        #
        # ENV:
        #   OMA_DRAWDOWN_GUARD=0/1
        #   OMA_MAX_DRAWDOWN_PCT=5.0
        #   OMA_DRAWDOWN_ACTION=COOLDOWN|RECOVERY|EMERGENCY_STOP
        #   OMA_DRAWDOWN_COOLDOWN_SEC=1800
        #   OMA_DRAWDOWN_TRIGGER_MIN_INTERVAL_SEC=60
        #   OMA_DRAWDOWN_NOTIFY=0/1
        self.drawdown_guard = _env_bool("OMA_DRAWDOWN_GUARD", False)
        self.max_drawdown_pct = abs(_env_float("OMA_MAX_DRAWDOWN_PCT", 5.0))
        self.drawdown_action = str(os.getenv("OMA_DRAWDOWN_ACTION", "RECOVERY")).strip().upper() or "RECOVERY"
        if self.drawdown_action not in ("COOLDOWN", "RECOVERY", "EMERGENCY_STOP"):
            self.drawdown_action = "RECOVERY"
        self.drawdown_cooldown_sec = _env_float("OMA_DRAWDOWN_COOLDOWN_SEC", 1800.0)
        self.drawdown_trigger_min_interval_sec = _env_float("OMA_DRAWDOWN_TRIGGER_MIN_INTERVAL_SEC", 60.0)
        self.drawdown_notify = _env_bool("OMA_DRAWDOWN_NOTIFY", True)

        # runtime state (not persisted)
        self._drawdown_base_equity_usdt: Optional[float] = None
        self._drawdown_latched: bool = False
        self._drawdown_last_trigger_ts: float = 0.0

        # Global entry cooldown (BUY only)
        self._global_entry_block_until_ts: float = 0.0
        self._global_entry_block_reason: str = ""


        # -----------------------------
        # Entry ceiling guard (regime-aware re-entry cap)
        # -----------------------------
        # 목적:
        # - 하락장/비상승장에서는 '직전 FULL EXIT 평균가(last_exit_price)'보다 비싼 가격에
        #   즉시 재매수(재진입)하는 것을 차단하여, 역마진/미세 손실 루프를 줄인다.
        #
        # 핵심 아이디어:
        # - last_exit_price를 기준으로 수수료/슬리피지/스프레드 버퍼를 감안한
        #   '재진입 한계가격(ceiling_price)'을 계산한다.
        # - 시장이 BULL(상승)로 판단되면 ceiling 차단을 완화(=차단하지 않음)할 수 있다.
        #
        # ENV:
        #   OMA_ENTRY_CEILING_GUARD=1
        #   OMA_ENTRY_CEILING_APPLY=BEAR|NON_BULL|ALWAYS   (default: NON_BULL)
        #   OMA_ENTRY_CEILING_FEE_RATE=0.0005             (default: OMA_FEE_RATE)
        #   OMA_ENTRY_CEILING_SLIPPAGE_GUARD_BPS=10       (default: OMA_EXIT_SLIPPAGE_GUARD_BPS)
        #   OMA_ENTRY_CEILING_SPREAD_GUARD_BPS=0
        #   OMA_ENTRY_CEILING_EXTRA_BPS=0
        #   OMA_ENTRY_CEILING_COOLDOWN_SEC=2
        #
        # Regime inference:
        #   OMA_REGIME_LOOKBACK_TICKS=300
        #   OMA_REGIME_BULL_PCT=0.4
        #   OMA_REGIME_BEAR_PCT=0.4
        #   OMA_REGIME_REQUIRE_MOMENTUM=1
        self.entry_ceiling_guard = _env_bool("OMA_ENTRY_CEILING_GUARD", True)
        self.entry_ceiling_apply = str(os.getenv("OMA_ENTRY_CEILING_APPLY", "NON_BULL")).strip().upper() or "NON_BULL"
        if self.entry_ceiling_apply not in ("BEAR", "NON_BULL", "ALWAYS"):
            self.entry_ceiling_apply = "NON_BULL"

        self.entry_ceiling_fee_rate = _env_float("OMA_ENTRY_CEILING_FEE_RATE", self.exit_fee_rate)
        self.entry_ceiling_slippage_guard_bps = _env_float("OMA_ENTRY_CEILING_SLIPPAGE_GUARD_BPS", self.exit_slippage_guard_bps)
        self.entry_ceiling_spread_guard_bps = _env_float("OMA_ENTRY_CEILING_SPREAD_GUARD_BPS", 0.0)
        self.entry_ceiling_extra_bps = _env_float("OMA_ENTRY_CEILING_EXTRA_BPS", 0.0)
        self.entry_ceiling_cooldown_sec = _env_float("OMA_ENTRY_CEILING_COOLDOWN_SEC", 2.0)
        # If > 0, ignore the "last_exit_price ceiling" after this many seconds since last full exit.
        self.entry_ceiling_max_age_sec = _env_float("OMA_ENTRY_CEILING_MAX_AGE_SEC", 1800.0)

        # Optional: relax the entry ceiling gradually within the max-age window.
        #
        # Motivation
        # - The strict ceiling (based on last_exit_price minus estimated costs) can block re-entry for too long
        #   when price stays above last_exit_price and drifts down slowly.
        # - With decay enabled, the ceiling linearly/exponentially moves upward from the strict ceiling
        #   toward last_exit_price as time passes. After max_age_sec, the ceiling guard is ignored (same as before).
        #
        # ENV:
        #   OMA_ENTRY_CEILING_DECAY_MODE=NONE|LINEAR|EXP   (default: LINEAR)
        #   OMA_ENTRY_CEILING_DECAY_HALF_LIFE_SEC=0       (EXP only; 0=auto ~ max_age/2)
        self.entry_ceiling_decay_mode = str(os.getenv("OMA_ENTRY_CEILING_DECAY_MODE", "LINEAR")).strip().upper() or "LINEAR"
        if self.entry_ceiling_decay_mode in ("OFF", "FALSE", "0"):
            self.entry_ceiling_decay_mode = "NONE"
        if self.entry_ceiling_decay_mode not in ("NONE", "LINEAR", "EXP"):
            self.entry_ceiling_decay_mode = "LINEAR"

        self.entry_ceiling_decay_half_life_sec = _env_float("OMA_ENTRY_CEILING_DECAY_HALF_LIFE_SEC", 0.0)

        self.regime_lookback_ticks = _env_int("OMA_REGIME_LOOKBACK_TICKS", 300)
        self.regime_bull_pct = abs(_env_float("OMA_REGIME_BULL_PCT", 0.4))
        self.regime_bear_pct = abs(_env_float("OMA_REGIME_BEAR_PCT", 0.4))
        self.regime_require_momentum = _env_bool("OMA_REGIME_REQUIRE_MOMENTUM", True)
        
        # Force ceiling guard even in BULL regime if exit was very recent (prevent rapid whipsaw)
        self.entry_ceiling_force_on_bull_sec = _env_float("OMA_ENTRY_CEILING_FORCE_ON_BULL_SEC", 300.0)

        # -----------------------------
        # Entry recent-high guard (avoid buying near N-hour highs in weak regimes)
        # -----------------------------
        # 목적:
        # - 하락장/비상승장에서 최근 N시간 고점 부근 추격매수를 차단한다.
        # - 다만 "진짜 돌파" 조건이면 예외 허용해 급등 구간 미스매치를 줄인다.
        #
        # ENV:
        #   OMA_ENTRY_RECENT_HIGH_GUARD=0/1
        #   OMA_ENTRY_RECENT_HIGH_APPLY=BEAR|NON_BULL|ALWAYS
        #   OMA_ENTRY_RECENT_HIGH_LOOKBACK_HOURS=24
        #   OMA_ENTRY_RECENT_HIGH_NEAR_PCT=0.8
        #   OMA_ENTRY_RECENT_HIGH_COOLDOWN_SEC=10
        #   OMA_ENTRY_RECENT_HIGH_CANDLE_UNIT_MIN=15
        #   OMA_ENTRY_RECENT_HIGH_CACHE_SEC=30
        #
        # Breakout 예외:
        #   OMA_ENTRY_RECENT_HIGH_BREAKOUT_ENABLED=1
        #   OMA_ENTRY_RECENT_HIGH_BREAKOUT_MARGIN_PCT=0.25
        #   OMA_ENTRY_RECENT_HIGH_BREAKOUT_REQUIRE_BULL=1
        #   OMA_ENTRY_RECENT_HIGH_BREAKOUT_MIN_REGIME_CHANGE_PCT=0.35
        #   OMA_ENTRY_RECENT_HIGH_BREAKOUT_MAX_SPREAD_BPS=18
        self.entry_recent_high_guard = _env_bool("OMA_ENTRY_RECENT_HIGH_GUARD", False)
        self.entry_recent_high_apply = str(os.getenv("OMA_ENTRY_RECENT_HIGH_APPLY", "NON_BULL")).strip().upper() or "NON_BULL"
        if self.entry_recent_high_apply not in ("BEAR", "NON_BULL", "ALWAYS"):
            self.entry_recent_high_apply = "NON_BULL"

        self.entry_recent_high_lookback_hours = _env_float("OMA_ENTRY_RECENT_HIGH_LOOKBACK_HOURS", 24.0)
        self.entry_recent_high_near_pct = _env_float("OMA_ENTRY_RECENT_HIGH_NEAR_PCT", 0.8)
        self.entry_recent_high_cooldown_sec = _env_float("OMA_ENTRY_RECENT_HIGH_COOLDOWN_SEC", 10.0)
        self.entry_recent_high_candle_unit_min = _env_int("OMA_ENTRY_RECENT_HIGH_CANDLE_UNIT_MIN", 15)
        self.entry_recent_high_cache_sec = _env_float("OMA_ENTRY_RECENT_HIGH_CACHE_SEC", 30.0)

        self.entry_recent_high_breakout_enabled = _env_bool("OMA_ENTRY_RECENT_HIGH_BREAKOUT_ENABLED", True)
        self.entry_recent_high_breakout_margin_pct = _env_float("OMA_ENTRY_RECENT_HIGH_BREAKOUT_MARGIN_PCT", 0.25)
        self.entry_recent_high_breakout_require_bull = _env_bool("OMA_ENTRY_RECENT_HIGH_BREAKOUT_REQUIRE_BULL", True)
        self.entry_recent_high_breakout_min_regime_change_pct = _env_float("OMA_ENTRY_RECENT_HIGH_BREAKOUT_MIN_REGIME_CHANGE_PCT", 0.35)
        self.entry_recent_high_breakout_max_spread_bps = _env_float("OMA_ENTRY_RECENT_HIGH_BREAKOUT_MAX_SPREAD_BPS", 18.0)
        self._entry_recent_high_cache: Dict[str, Dict[str, Any]] = {}


        # -----------------------------
        # Entry qty/price guard (oversize protection)
        # -----------------------------
        # 문제 배경:
        # - 저단가 코인에 큰 원금이 배정되면 매수/매도 qty가 과도하게 커지고(=orderbook을 깊게 긁음),
        #   예상가 대비 체결가가 불리해져 역마진/부분체결/연속 손절로 이어질 수 있다.
        #
        # 구현:
        # - qty_est = buy_usdt / expected_price
        # - qty_est > max_qty 이면 BUY intent 차단 + 짧은 쿨다운 부여.
        #
        # ENV:
        #   OMA_ENTRY_QTY_GUARD=1
        #   OMA_ENTRY_MAX_QTY=1000
        #   OMA_ENTRY_QTY_COOLDOWN_SEC=2
        self.entry_qty_guard = _env_bool("OMA_ENTRY_QTY_GUARD", True)
        self.entry_max_qty = _env_float("OMA_ENTRY_MAX_QTY", 1000000.0)
        self.entry_qty_cooldown_sec = _env_float("OMA_ENTRY_QTY_COOLDOWN_SEC", 2.0)

        # GLOBAL ENTRY THROTTLES (reduce bursts/latency when many markets fire at once)
        # - OMA_ENTRY_GLOBAL_GAP_SEC: minimum seconds between BUY submissions across the whole system
        # - OMA_MAX_PENDING_ORDERS_TOTAL: block new BUYs when too many pending orders exist across markets
        self.entry_global_gap_sec = _env_float("OMA_ENTRY_GLOBAL_GAP_SEC", 0.0)
        self.max_pending_orders_total = _env_int("OMA_MAX_PENDING_ORDERS_TOTAL", 0)

        # runtime state for global throttles
        self._last_entry_submit_ts = 0.0


        # -----------------------------
        # Capital: fixed principal (non-compounding stake)
        # -----------------------------
        # - True  : 각 ACTIVE 시장에 "초기 배정 원금"을 고정(stake)하고,
        #          이익은 원금에 합산하지 않음(=복리 미적용).
        #          pingpong이 연속으로 돌아갈 때 주문 금액이 0으로 떨어지지 않도록 한다.
        # - False : 기존 방식(현재 배치된 금액을 제외한 "추가 배치 가능 금액"만 분배)
        self.fixed_principal = _env_bool("OMA_FIXED_PRINCIPAL", True)
        self._principal_total_usdt: Optional[float] = None
        self._principal_base_equity_usdt: Optional[float] = None

        # -----------------------------
        # Boot safety
        # -----------------------------
        self.cancel_wait_orders_on_boot = _env_bool("OMA_CANCEL_WAIT_ORDERS_ON_BOOT", False)
        self.recovery_auto_liquidate = _env_bool("OMA_RECOVERY_AUTO_LIQUIDATE", False)

        # Recovery policy (global)
        # - HOLD: 자동 청산 없음(수동/조건부를 권장)
        # - CONDITIONAL: 조건 만족 시 회수(청산)
        # - AUTO: 즉시 청산
        self.recovery_policy = str(os.getenv("OMA_RECOVERY_POLICY", "HOLD")).strip().upper() or "HOLD"
        if self.recovery_auto_liquidate:
            self.recovery_policy = "AUTO"
        if self.recovery_policy not in ("HOLD", "CONDITIONAL", "AUTO"):
            self.recovery_policy = "HOLD"

        self.recovery_cond_max_hold_sec = _env_float("OMA_RECOVERY_COND_MAX_HOLD_SEC", 1800.0)  # 30 min
        # 음수/양수 입력 모두 허용: -3 또는 3 → 3% 손절 트리거
        self.recovery_cond_stoploss_pct = abs(_env_float("OMA_RECOVERY_COND_STOPLOSS_PCT", 3.0))
        # 회수 주문 최소 가치(추정). Bybit 최소 주문과 동일/상향 권장.
        self.recovery_min_value_usdt = _env_float("OMA_RECOVERY_MIN_VALUE_USDT", self.min_order_usdt)

        # -----------------------------
        # Equity snapshot (LIVE)
        # -----------------------------
        self._accounts_snapshot: List[Dict[str, Any]] = []
        self._last_cash_usdt: float = 0.0
        self._last_deployed_usdt: float = 0.0
        self._last_equity_usdt: float = 0.0
        self._last_equity_ts: float = 0.0

        # allocation logging throttle
        self._last_alloc_log_ts: float = 0.0
        self._last_alloc_sig: str = ""

        # tick loop throttle intervals (reduce per-tick overhead)
        self._rebalance_interval_sec: float = _env_float("OMA_REBALANCE_INTERVAL_SEC", 10.0)
        self._last_rebalance_ts: float = 0.0
        self._ladder_sync_interval_sec: float = _env_float("LADDER_SYNC_INTERVAL_SEC", 10.0)
        self._last_ladder_sync_ts: float = 0.0
        self._portfolio_risk_interval_sec: float = 30.0
        self._last_portfolio_risk_ts: float = 0.0
        self._smart_alert_interval_sec: float = 60.0
        self._last_smart_alert_ts: float = 0.0

        # -----------------------------
        # Bybit trade client (LIVE)
        # -----------------------------
        self.trade_client = None
        self.order_fsm = None

        if self.trading_mode == "LIVE":
            ak = Q.get_access_key() or os.getenv("BYBIT_API_KEY", "")
            sk = Q.get_secret_key() or os.getenv("BYBIT_API_SECRET", "")
            if not ak or not sk:
                if self.strict_live:
                    raise RuntimeError("STRICT_LIVE=1 but BYBIT_API_KEY/BYBIT_API_SECRET missing")
            else:
                from app.integrations.bybit_trade import BybitTradeClient as BybitTradeClient
                from app.manager.order_state_machine import OrderStateMachine

                self.trade_client = BybitTradeClient(
                    api_key=ak,
                    api_secret=sk,
                    timeout=_env_float("BYBIT_TIMEOUT", 10.0),
                    category=self.bybit_v5_category,
                )
                self.order_fsm = OrderStateMachine(client=self.trade_client, ledger=self.ledger)
                self.order_fsm._sell_fill_callbacks.append(self._on_sell_filled)
                self.order_fsm._buy_fill_callbacks.append(self._on_buy_filled)

        elif self.trading_mode == "PAPER":
            from app.integrations.paper_trade_client import PaperTradeClient
            from app.manager.order_state_machine import OrderStateMachine

            self.trade_client = PaperTradeClient(
                initial_usdt=float(os.getenv("DRY_INITIAL_USDT", "1000")),
                fee_rate=_env_float("PAPER_FEE_RATE", 0.001),
                slippage_bps=_env_float("PAPER_SLIPPAGE_BPS", 5.0),  # ★ [2026-06-24] paper 슬리피지 모델
            )
            self.order_fsm = OrderStateMachine(client=self.trade_client, ledger=self.ledger)
            self.order_fsm._sell_fill_callbacks.append(self._on_sell_filled)
            self.order_fsm._buy_fill_callbacks.append(self._on_buy_filled)
            logger.info("[BOOT] Paper Trading mode: $%.2f virtual balance", self.trade_client._usdt_balance)

        # -----------------------------
        # WATCH refresh loop
        # -----------------------------
        self._watch_task: asyncio.Task | None = None
        self.watch_last_refresh_ts: float | None = None
        self.watch_refresh_interval_sec = _env_int("OMA_WATCH_REFRESH_MIN", 15) * 60

        # -----------------------------
        # Tick loop
        # -----------------------------
        self._tick_task: asyncio.Task | None = None
        self.tick_interval_sec = _env_float("TICK_INTERVAL_SEC", 1.0)

        # -----------------------------
        # Known markets cache (orphan validation)
        # -----------------------------
        self._known_markets: Set[str] = set()
        
        self.skip_currencies = _env_csv_upper("OMA_SKIP_CURRENCIES") or ["APENFT"]


        # Log/notify throttles (avoid ledger spam on persistent blocked/soft conditions)
        self._block_log_last: Dict[Tuple[str, str, str], float] = {}
        self._block_log_interval_sec = _env_float("OMA_BLOCK_LOG_INTERVAL_SEC", 30.0)
        self._soft_notice_last: Dict[Tuple[str, str], float] = {}
        self._soft_notice_interval_sec = _env_float("OMA_SOFT_NOTICE_INTERVAL_SEC", 600.0)
        self.emergency_manual_override: bool = False


        # -----------------------------
        # Night Mode (시간대별 진입/SL 조절)
        # -----------------------------
        self.night_mode_enabled: bool = _env_bool("OMA_NIGHT_MODE_ENABLED", False)
        self.night_mode_start_hour: int = _env_int("OMA_NIGHT_MODE_START_HOUR", 2)
        self.night_mode_end_hour: int = _env_int("OMA_NIGHT_MODE_END_HOUR", 9)
        self.night_mode_entry_score_boost_pct: float = _env_float("OMA_NIGHT_MODE_ENTRY_SCORE_BOOST_PCT", 30.0)
        self.night_mode_sl_multiplier: float = _env_float("OMA_NIGHT_MODE_SL_MULTIPLIER", 1.5)

        # -----------------------------
        # Reserved Queue / Autopilot (UI)
        # -----------------------------
        # NOTE:
        # - Reserved/Autopilot settings are controlled from the dashboard (right-bottom panel).
        # - These defaults can be overridden by runtime/ui_settings.json via _load_ui_settings().
        self.reserved_pingpong_n: int = _env_int("OMA_RESERVED_PINGPONG_N", 5)
        self.reserved_autoloop_n: int = _env_int("OMA_RESERVED_AUTOLOOP_N", 5)
        self.reserved_ladder_n: int = _env_int("OMA_RESERVED_LADDER_N", 2)
        self.reserved_lightning_n: int = _env_int("OMA_RESERVED_LIGHTNING_N", 3)
        self.reserved_gazua_n: int = _env_int("OMA_RESERVED_GAZUA_N", 2)
        self.reserved_contrarian_n: int = _env_int("OMA_RESERVED_CONTRARIAN_N", 2)
        self.reserved_sniper_n: int = _env_int("OMA_RESERVED_SNIPER_N", 3)
        self.reserved_whale_n: int = _env_int("OMA_RESERVED_WHALE_N", 0)
        # SNIPER(s) Scope 기본 설정
        self.autopilot_scope_rotation_enabled: bool = _env_bool("OMA_AUTOPILOT_SCOPE_ROTATION_ENABLED", True)
        self.autopilot_scope_idle_min: int = max(2, _env_int("OMA_AUTOPILOT_SCOPE_IDLE_MIN", 2))
        deploy_mode = str(os.getenv("OMA_AUTOPILOT_SCOPE_DEPLOY_MODE", "wait") or "wait").strip().lower()
        self.autopilot_scope_deploy_mode: str = deploy_mode if deploy_mode in ("wait", "market", "trap") else "wait"
        # SNIPER(s) Scope 자동충원 목표 슬롯 (SNIPER와 분리)
        self.autopilot_scope_target_n: int = _env_int("OMA_AUTOPILOT_SCOPE_TARGET_N", self.reserved_sniper_n)
        # Trap TP 미체결 타임아웃 (시간 단위, 0=비활성)
        self.autopilot_scope_trap_tp_timeout_hours: float = _env_float("OMA_SCOPE_TRAP_TP_TIMEOUT_H", 4.0)
        # Scope 매도 후 재등장 쿨다운 (분 단위)
        self.autopilot_scope_cooldown_min: int = _env_int("OMA_SCOPE_COOLDOWN_MIN", 60)
        # 점수 기반 유동 쿨다운 (높은 점수 → 빠른 재등장)
        self.autopilot_scope_adaptive_cd: bool = _env_bool("OMA_SCOPE_ADAPTIVE_CD", True)

        self.longshort_scope_min_price: float = _env_float("OMA_SCOPE_MIN_PRICE", 0.0)
        self.longshort_scope_max_price: float = _env_float("OMA_SCOPE_MAX_PRICE", 0.0)
        
        # [2026-01-31] SNIPER 급등/역행 스캐너 설정
        self.sniper_min_surge_pct: float = _env_float("OMA_SNIPER_MIN_SURGE_PCT", 5.0)  # 최소 급등률/역행강도 %
        self.sniper_scan_timeframe: str = os.getenv("OMA_SNIPER_SCAN_TIMEFRAME", "1h")  # 5m, 15m, 1h, 4h
        self.sniper_scan_mode: str = os.getenv("OMA_SNIPER_SCAN_MODE", "relative")  # absolute, relative, both
        
        self.reserved_apply_suggested_budget: bool = _env_bool("OMA_RESERVED_APPLY_SUGGESTED_BUDGET", True)
        self.reserved_promote_to_active: bool = _env_bool("OMA_RESERVED_PROMOTE_TO_ACTIVE", False)

        self.autopilot_enabled: bool = _env_bool("OMA_AUTOPILOT_ENABLED", False)
        self.autopilot_auto_approve: bool = _env_bool("OMA_AUTOPILOT_AUTO_APPROVE", False)

        # [2026-02-21] Global Profit Take + common SL safety floor
        self.global_profit_take: bool = _env_bool("OMA_GLOBAL_PROFIT_TAKE", False)
        self.global_profit_pct: float = max(1.0, min(100.0, _env_float("OMA_GLOBAL_PROFIT_PCT", 5.0)))
        self.global_profit_interval_min: float = max(1.0, min(60.0, _env_float("OMA_GLOBAL_PROFIT_INTERVAL_MIN", 10.0)))
        self.global_min_sl_pct: float = _env_float("OMA_GLOBAL_MIN_SL_PCT", -2.5)
        if self.global_min_sl_pct > 0:
            self.global_min_sl_pct = -abs(self.global_min_sl_pct)
        self.global_min_sl_pct = max(-95.0, min(-0.1, float(self.global_min_sl_pct)))
        os.environ["OMA_GLOBAL_MIN_SL_PCT"] = str(self.global_min_sl_pct)
        
        # [2026-03-23] 5가지 지능형 거래 기능
        # ② 동적 매수 규모 (PnL 기반 선형 감소)
        self.dynamic_size_mult_enabled: bool = _env_bool("OMA_DYNAMIC_SIZE_MULT_ENABLED", True)
        # ① 레짐별 전략 예산 스위칭
        self.regime_per_strategy_enabled: bool = _env_bool("OMA_REGIME_PER_STRATEGY_ENABLED", False)
        # ①② 동시 활성화 불가 — 두 multiplier 곱연산 시 최대 0.32x 감소로 의도치 않은 주문 축소 발생
        # 재시작 시에도 동일하게 상호 배타 적용: 기본 ON인 ② 우선, ① 자동 OFF
        if self.dynamic_size_mult_enabled and self.regime_per_strategy_enabled:
            self.regime_per_strategy_enabled = False
            logger.warning("[SmartRisk] ①②동시 활성화 감지 → ① 레짐스위칭 자동 OFF (② 동적규모 우선)")
        self._regime_strategy_manager = None   # lazy init
        # ③ 단일 코인 집중도 한도
        self.concentration_limit_enabled: bool = _env_bool("OMA_CONCENTRATION_LIMIT_ENABLED", False)
        self.concentration_limit_pct: float = max(5.0, min(50.0, _env_float("OMA_CONCENTRATION_LIMIT_PCT", 15.0)))
        # ④ 수익 자동 락인 (부분매도)
        self.profit_lock_enabled: bool = _env_bool("OMA_PROFIT_LOCK_ENABLED", False)
        self.profit_lock_trigger_pct: float = max(1.0, _env_float("OMA_PROFIT_LOCK_TRIGGER_PCT", 10.0))
        self.profit_lock_sell_ratio: float = max(0.05, min(0.95, _env_float("OMA_PROFIT_LOCK_SELL_RATIO", 0.3)))
        self.profit_lock_cooldown_sec: float = max(60.0, _env_float("OMA_PROFIT_LOCK_COOLDOWN_SEC", 3600.0))
        # [2026-03-24] Peak Drawdown Guard — TP 근접 후 반전 시 자동 매도
        self.peak_drawdown_guard_enabled: bool = _env_bool("OMA_PEAK_DRAWDOWN_GUARD_ENABLED", True)
        self.peak_drawdown_activation_pct: float = max(10.0, min(100.0, _env_float("OMA_PEAK_DRAWDOWN_ACTIVATION_PCT", 80.0)))
        self.peak_drawdown_trigger_pct: float = max(10.0, min(90.0, _env_float("OMA_PEAK_DRAWDOWN_TRIGGER_PCT", 50.0)))
        self.peak_drawdown_min_profit_pct: float = max(0.1, _env_float("OMA_PEAK_DRAWDOWN_MIN_PROFIT_PCT", 0.3))

        # [2026-02-02] Auto Engine Start on Boot
        self.auto_engine_start: bool = _env_bool("OMA_AUTO_ENGINE_START", False)

        # --- Per-strategy auto-approve toggles (fallback to autopilot_auto_approve) ---
        # [FIX] 기본값을 False로 변경 - 명시적으로 켜야만 작동하도록
        self.autopilot_auto_approve_pingpong: bool = _env_bool("OMA_AUTOPILOT_AUTO_APPROVE_PINGPONG", False)
        self.autopilot_auto_approve_autoloop: bool = _env_bool("OMA_AUTOPILOT_AUTO_APPROVE_AUTOLOOP", False)
        self.autopilot_auto_approve_ladder: bool = _env_bool("OMA_AUTOPILOT_AUTO_APPROVE_LADDER", False)
        self.autopilot_auto_approve_lightning: bool = _env_bool("OMA_AUTOPILOT_AUTO_APPROVE_LIGHTNING", False)
        self.autopilot_auto_approve_gazua: bool = _env_bool("OMA_AUTOPILOT_AUTO_APPROVE_GAZUA", False)
        self.autopilot_auto_approve_contrarian: bool = _env_bool("OMA_AUTOPILOT_AUTO_APPROVE_CONTRARIAN", False)
        self.autopilot_auto_approve_sniper: bool = _env_bool("OMA_AUTOPILOT_AUTO_APPROVE_SNIPER", False)
        
        # [2026-02-06] BTC Guard Mode - BTC 하락 시 CONTRARIAN 외 매입 차단
        self.btc_guard_enabled: bool = _env_bool("OMA_BTC_GUARD_ENABLED", True)  # UI 메인 토글
        self.btc_guard_mode: bool = False  # 현재 가드 상태 (런타임 동적)
        self.btc_guard_threshold: float = _env_float("OMA_BTC_GUARD_THRESHOLD", 0.5)  # BTC signal strength 임계값
        self.btc_guard_down_5m_pct: float = abs(_env_float("OMA_BTC_GUARD_DOWN_5M_PCT", 2.0))
        self.btc_guard_down_15m_pct: float = abs(_env_float("OMA_BTC_GUARD_DOWN_15M_PCT", 5.0))
        self.btc_guard_recover_5m_pct: float = abs(_env_float("OMA_BTC_GUARD_RECOVER_5M_PCT", 1.2))
        self.btc_guard_recover_15m_pct: float = abs(_env_float("OMA_BTC_GUARD_RECOVER_15M_PCT", 3.0))
        self.btc_guard_trail_tighten_ratio: float = max(
            0.1,
            min(0.95, float(_env_float("OMA_BTC_GUARD_TRAIL_TIGHTEN_RATIO", 0.5) or 0.5)),
        )
        self._pre_guard_auto_approve: Dict[str, bool] = {}  # 복원용 이전 상태
        self._pre_guard_trailing_stops: Dict[str, Dict[str, float]] = {}  # Trailing Stop 복원용

        # [2026-03-18] Recovery Boost — 하락 후 반등 시 빠른 회수 + 추가 이윤
        self.recovery_boost_enabled: bool = _env_bool("OMA_RECOVERY_BOOST_ENABLED", True)
        self.recovery_boost_active: bool = False
        self.recovery_boost_activated_ts: float = 0.0
        self.recovery_boost_duration_sec: float = _env_float("OMA_RECOVERY_BOOST_DURATION_SEC", 1800.0)
        self.recovery_boost_quick_tp_pct: float = _env_float("OMA_RECOVERY_BOOST_QUICK_TP_PCT", 0.5)
        self.recovery_boost_momentum_tp_mult: float = _env_float("OMA_RECOVERY_BOOST_MOMENTUM_TP_MULT", 1.5)
        self.recovery_boost_budget_mult: float = _env_float("OMA_RECOVERY_BOOST_BUDGET_MULT", 1.3)
        self._pre_boost_tp: Dict[str, Dict[str, float]] = {}

        # [Phase 3] Sniper Fast Lane — 급락 감지 즉시 진입
        self._sniper_fast_lane = None
        self._last_fast_lane_ts: float = 0.0
        self._fast_lane_inflight: bool = False
        try:
            from app.monitor.sniper_fast_lane import SniperFastLane
            _fl = SniperFastLane()
            if _fl.enabled:
                self._sniper_fast_lane = _fl
                logger.info("[HyperSystem] Sniper Fast Lane ENABLED")
        except (AttributeError, TypeError) as _fl_err:
            logger.debug("[HyperSystem] Fast Lane init skip: %s", _fl_err)

        # [2026-02-04] 전략별 백테스트 가중치 (0.0~1.0, 슬롯 추천에 반영)
        # 실거래 데이터가 적을 때 백테스트 결과를 얼마나 반영할지 결정
        # 0.0 = 백테스트 무시 (실거래만), 1.0 = 백테스트만 사용
        self.backtest_weight_pingpong: float = _env_float("OMA_BACKTEST_WEIGHT_PINGPONG", 0.10)  # 빠른 데이터
        self.backtest_weight_autoloop: float = _env_float("OMA_BACKTEST_WEIGHT_AUTOLOOP", 0.15)
        self.backtest_weight_ladder: float = _env_float("OMA_BACKTEST_WEIGHT_LADDER", 0.30)      # 느린 데이터
        self.backtest_weight_lightning: float = _env_float("OMA_BACKTEST_WEIGHT_LIGHTNING", 0.15)
        self.backtest_weight_gazua: float = _env_float("OMA_BACKTEST_WEIGHT_GAZUA", 0.35)        # 매우 느림
        self.backtest_weight_contrarian: float = _env_float("OMA_BACKTEST_WEIGHT_CONTRARIAN", 0.20)
        self.backtest_weight_sniper: float = _env_float("OMA_BACKTEST_WEIGHT_SNIPER", 0.30)       # 느린 데이터

        # --- NEW: AI Gate / Demote options ---
        self.autopilot_ai_gate_enabled: bool = _env_bool("OMA_AUTOPILOT_AI_GATE_ENABLED", False)
        self.autopilot_ai_gate_threshold: float = _env_float("OMA_AUTOPILOT_AI_GATE_THRESHOLD", 0.55)

        self.autopilot_ai_demote_enabled: bool = _env_bool("OMA_AUTOPILOT_AI_DEMOTE_ENABLED", False)
        self.autopilot_ai_demote_threshold: float = _env_float("OMA_AUTOPILOT_AI_DEMOTE_THRESHOLD", 0.45)

        self.time_zone_optimizer_enabled: bool = _env_bool("OMA_TIME_ZONE_OPTIMIZER_ENABLED", False)

        self.autopilot_idle_demote_enabled: bool = _env_bool("OMA_AUTOPILOT_IDLE_DEMOTE_ENABLED", True)
        self.autopilot_idle_demote_min: int = _env_int("OMA_AUTOPILOT_IDLE_DEMOTE_MIN", 180)  # minutes
        self.autopilot_idle_demote_overrides: Dict[str, int] = {}  # per-strategy overrides

        # [2026-02-01] 24시간 무거래 → LongHold 자동 전환
        # AUTOLOOP/PINGPONG 제외, 나머지 전략은 24시간 무거래 시 LongHold로 이동
        self.autopilot_idle_to_longhold_enabled: bool = _env_bool("OMA_AUTOPILOT_IDLE_TO_LONGHOLD_ENABLED", True)
        self.autopilot_idle_to_longhold_hours: int = _env_int("OMA_AUTOPILOT_IDLE_TO_LONGHOLD_HOURS", 24)

        self.autopilot_eval_interval_sec: int = _env_int("OMA_AUTOPILOT_EVAL_INTERVAL_SEC", 300)
        self.autopilot_grace_sec: int = _env_int("OMA_AUTOPILOT_GRACE_SEC", 900)
        self.autopilot_demote_max_total: int = _env_int("OMA_AUTOPILOT_DEMOTE_MAX_TOTAL", 2)
        self.autopilot_demote_max_per_strategy: int = _env_int("OMA_AUTOPILOT_DEMOTE_MAX_PER_STRATEGY", 1)

        self.autopilot_window_enabled: bool = _env_bool("OMA_AUTOPILOT_WINDOW_ENABLED", False)
        self.autopilot_window_start: str = str(os.getenv("OMA_AUTOPILOT_WINDOW_START", "22:00") or "22:00")
        self.autopilot_window_end: str = str(os.getenv("OMA_AUTOPILOT_WINDOW_END", "08:00") or "08:00")

        # Future rules (UI already exposes toggles; backend logic may be extended)
        self.autopilot_guard_demote_enabled: bool = _env_bool("OMA_AUTOPILOT_GUARD_DEMOTE_ENABLED", False)
        self.autopilot_guard_demote_window_min: int = _env_int("OMA_AUTOPILOT_GUARD_DEMOTE_WINDOW_MIN", 30)
        self.autopilot_guard_demote_n: int = _env_int("OMA_AUTOPILOT_GUARD_DEMOTE_N", 12)

        self.autopilot_signal_miss_enabled: bool = _env_bool("OMA_AUTOPILOT_SIGNAL_MISS_ENABLED", False)
        self.autopilot_signal_miss_window_min: int = _env_int("OMA_AUTOPILOT_SIGNAL_MISS_WINDOW_MIN", 30)
        self.autopilot_signal_miss_min_attempts: int = _env_int("OMA_AUTOPILOT_SIGNAL_MISS_MIN_ATTEMPTS", 6)

        # Performance / churn demotion (experience-based defaults)
        # - 목적: 계속 사고팔아도 이익이 안 나는(수수료만 태우는) 코인을 자동 퇴출
        # - 조건은 "많이 거래했는데(net) 이익이 거의/전혀 없는가"에 초점을 둔다.
        self.autopilot_perf_demote_enabled: bool = _env_bool("OMA_AUTOPILOT_PERF_DEMOTE_ENABLED", True)
        self.autopilot_perf_window_min: int = _env_int("OMA_AUTOPILOT_PERF_WINDOW_MIN", 90)
        self.autopilot_perf_min_trades: int = _env_int("OMA_AUTOPILOT_PERF_MIN_TRADES", 6)
        self.autopilot_perf_min_sells: int = _env_int("OMA_AUTOPILOT_PERF_MIN_SELLS", 2)
        self.autopilot_perf_min_net_cash_usdt: float = _env_float("OMA_AUTOPILOT_PERF_MIN_NET_CASH_USDT", 1000.0)
        self.autopilot_perf_min_net_cash_per_trade: float = _env_float("OMA_AUTOPILOT_PERF_MIN_NET_CASH_PER_TRADE_USDT", 0.0)

        # [2026-02-01] 자동 먼지 청소 설정
        self.dust_vacuum_enabled: bool = _env_bool("OMA_DUST_VACUUM_ENABLED", False)
        self.dust_vacuum_daily_count: int = _env_int("OMA_DUST_VACUUM_DAILY_COUNT", 1)  # 하루 N회
        self.dust_vacuum_threshold_usdt: float = _env_float("OMA_DUST_VACUUM_THRESHOLD_USDT", 5.0)
        self.dust_vacuum_last_run_date: str = ""  # YYYY-MM-DD
        self.dust_vacuum_today_count: int = 0  # 오늘 실행 횟수

        # Cooldown after demotion
        # - 목적: 강등된 코인이 즉시 다시 후보로 잡혀서 "퇴출→재진입" 루프가 생기는 것 방지
        self.autopilot_cooldown_min: int = _env_int("OMA_AUTOPILOT_COOLDOWN_MIN", 180)
        self.autopilot_cooldown_path: str = str(os.getenv("OMA_AUTOPILOT_COOLDOWN_PATH", "runtime/autopilot_cooldown.json") or "runtime/autopilot_cooldown.json")
        self.autopilot_cooldown: Dict[str, Dict[str, Any]] = {}
        try:
            self._load_autopilot_cooldown()
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            logger.warning("[BOOT] autopilot cooldown load failed - demote/re-entry loop prevention inactive: %s", exc, exc_info=True)

        # Runtime state (not persisted)
        self._autopilot_task: asyncio.Task | None = None
        self._autopilot_inflight: bool = False
        self._autopilot_lock: asyncio.Lock = asyncio.Lock()
        self.autopilot_last_run_ts: float | None = None
        self.autopilot_last_result: Any = None

        # Scan gate: build_reserved_candidates 동시 실행을 직렬화.
        # 복수 호출(autopilot + prewarm)이 동시에 실행되면 rate limiter / GIL 경합으로
        # tick 지연이 발생한다. Lock으로 한 번에 하나만 실행되도록 보장.
        self._scan_gate: asyncio.Lock = asyncio.Lock()
        # 전용 scan executor: build_reserved_candidates / prewarm을 기본 asyncio 스레드풀과
        # 분리하여 tick loop의 asyncio.to_thread 호출과 스레드 경합 방지.
        # max_workers=2 → scan 1개 + 예비 1개 (rate limiter 대기 중 다음 scan 준비)
        _scan_workers = int(os.getenv("OMA_SCAN_THREAD_WORKERS", "2"))
        self._scan_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=_scan_workers,
            thread_name_prefix="oma_scan",
        )
        # [PERF] 백그라운드 I/O 전용 executor (2026-03-21)
        # reconcile, rebalance, ladder_sync 등 12개 백그라운드 태스크를
        # 기본 asyncio executor와 분리하여 order_fsm 크리티컬 패스 보호
        self._bg_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=4,
            thread_name_prefix="oma_bg",
        )
        # [PERF] order_fsm 전용 executor (2026-03-21)
        # 54개 마켓이 asyncio.gather로 동시에 to_thread(order_fsm) 호출 시
        # 기본 executor에 최대 32개 스레드가 생성되어 GIL 경합 유발.
        # 전용 executor(8 workers)로 격리하여 스레드 폭증 방지.
        self._order_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=8,
            thread_name_prefix="oma_order",
        )

        # [PERF] default asyncio executor 제한 (2026-03-21)
        # asyncio.to_thread() / run_in_executor(None, ...) 호출 시 사용되는 기본 executor.
        # Python 기본값 = min(32, cpu_count+4) = 24 (20코어 서버) → 스레드 폭증 유발.
        # 전용 executor(_bg/order/scan)로 크리티컬 패스를 이미 분리했으므로
        # 나머지(longhold_poll, global_profit, AI trainer 등)는 8개면 충분.
        self._default_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=8,
            thread_name_prefix="oma_default",
        )

        self._ai_task: asyncio.Task | None = None
        self.ai_retrain_threshold: float = _env_float("OMA_AI_RETRAIN_THRESHOLD", 0.6)

        # [Ladder Auto-Tuner] periodic tuning
        self._ladder_tune_interval_sec = _env_int("LADDER_TUNE_INTERVAL_MIN", 60) * 60
        self._ladder_tune_last_ts: float = 0.0

        # Performance monitoring
        self._last_tick_duration: float = 0.0
        self._tick_count: int = 0
        
        # Cached ledger PnL (updated every 30s in tick loop to avoid heavy status() calls)
        self._cached_ledger_pnl: float = 0.0
        self._cached_ledger_pnl_ts: float = 0.0

        # -----------------------------
        # UI settings persistence (dashboard overrides > env)
        # -----------------------------
        # - ENV는 "기본값"으로만 취급한다.
        # - Dashboard/API에서 조정한 safety/guard 파라미터는 런타임 파일에 저장하여
        #   재시작 후에도 그대로 복원한다.
        #
        # NOTE: per-market overrides는 ctx.controls.guards 로 저장되며
        #       runtime/context_state.json 복원 경로를 그대로 사용한다.
        self.ui_settings_path = os.getenv("OMA_UI_SETTINGS_PATH", "runtime/ui_settings.json")
        self._ui_settings_loaded: bool = False
        self._ui_guard_overrides: Dict[str, Any] = {}
        try:
            self._load_ui_settings()
            print(f"[BOOT] ui_settings loaded: auto_engine_start={getattr(self, 'auto_engine_start', 'NOT SET')}")
        except (KeyError, AttributeError, TypeError) as _e:
            print(f"[BOOT] ui_settings load FAILED: {_e}")
            logger.warning("[BOOT] ui_settings load failed: %s", _e, exc_info=True)
        
        # LADDER manager (reservation-based)
        try:
            from app.manager.ladder_manager import LadderManager
            self.ladder_manager = LadderManager(system=self)
        except (ImportError, AttributeError, TypeError) as exc:
            logger.error("[BOOT] LadderManager init FAILED: %s", exc, exc_info=True)
            self.ladder_manager = None

        # Quick Trade manager (수동 즉시/조건부 거래)
        try:
            from app.manager.quick_trade_manager import QuickTradeManager
            self.quick_trade_manager = QuickTradeManager(system=self)
        except (ImportError, AttributeError, TypeError) as exc:
            logger.error("[BOOT] QuickTradeManager init FAILED: %s", exc, exc_info=True)
            self.quick_trade_manager = None
        
        # -----------------------------
        # Portfolio Risk Manager (포트폴리오 레벨 리스크 관리)
        # -----------------------------
        # - 일일 손실 한도
        # - Circuit Breaker (과도한 손실 시 자동 중단)
        # - 코인 상관관계 체크 (한 방향 몰빵 방지)
        self.portfolio_risk_manager = get_portfolio_risk_manager()
        # [2026-03-23] UI Guard 설정값을 PRM에 동기화
        self.portfolio_risk_manager.sync_from_system(self)

        # -----------------------------
        # Smart Alert Manager (스마트 알림)
        # -----------------------------
        # - 연속 손실 경고 (3연패 알림)
        # - 이상 거래 탐지 (평균 대비 큰 손실)
        # - 일일 요약 리포트 자동 발송
        self.smart_alert_manager = get_smart_alert_manager()

        # LongHold (LADDER/GAZUA advisory) — periodic Telegram alerts
        # - Enabled by default; no effect if TELEGRAM env is not set.
        self.longhold_alerts_enabled = _env_bool("OMA_LONGHOLD_ALERTS", True)
        self.longhold_poll_interval_sec = _env_float("OMA_LONGHOLD_POLL_INTERVAL_SEC", 30.0)
        self._last_longhold_poll_ts = 0.0
        self._longhold_poll_inflight = False
        self._longhold_poll_lock: asyncio.Lock = asyncio.Lock()
        self._global_profit_poll_inflight: bool = False
        self._global_profit_poll_lock: asyncio.Lock = asyncio.Lock()
        
        # [2026-02-01] AutopilotManager (모든 전략 지원 + SNIPER 급등 스캐너)
        self.autopilot_manager = None
        try:
            from app.manager.autopilot_manager import AutopilotManager
            self.autopilot_manager = AutopilotManager(system=self)
        except (ImportError, AttributeError, TypeError) as e:
            self.autopilot_manager = None
            try:
                self.ledger.append("AUTOPILOT_MANAGER_INIT_ERROR", error=str(e))
            except (AttributeError, TypeError, ValueError) as exc:
                logger.warning("[BOOT] AutopilotManager ledger append for init error failed: %s", exc, exc_info=True)

        # [TRIAGE MODE] 포트폴리오 긴급 복구 시스템
        self.triage_manager = None
        self._triage_entry_blocked: bool = False   # _handle_intent에서 BUY 차단 플래그
        self._triage_reserved_usdt: float = 0.0   # DCA 자본 예약 (면제전략 가용자본에서 차감)
        self._last_triage_poll_ts: float = 0.0
        self._triage_poll_inflight: bool = False

        # [2026-03-25] tick_loop inflight flags 초기화 (getattr fallback 제거)
        self._reconcile_inflight: bool = False
        self._btc_guard_inflight: bool = False
        self._rebalance_inflight: bool = False
        self._ladder_sync_inflight: bool = False
        self._ledger_pnl_inflight: bool = False
        self._portfolio_risk_inflight: bool = False
        self._smart_alert_inflight: bool = False
        self._ladder_tune_inflight: bool = False
        try:
            from app.manager.triage_manager import TriageManager
            self.triage_manager = TriageManager()
            # 재시작 시 영속 상태 복원
            self.triage_manager.load_state()
            if self.triage_manager.is_active():
                if self.triage_manager.settings.get("enabled"):
                    # enabled=True: 활성 상태 복원
                    self._triage_entry_blocked = True
                    logger.info("[HyperSystem] Triage mode RESTORED from state file (state=%s)", self.triage_manager.state)
                else:
                    # enabled=False: 테스트 등으로 남은 잔여 상태 → 자동 종료
                    logger.info("[HyperSystem] Triage state found but enabled=False → auto-exiting (state=%s)", self.triage_manager.state)
                    self.triage_manager.state = TriageManager.STATE_NORMAL
                    self.triage_manager.save_state()
            logger.info("[HyperSystem] TriageManager initialized (state=%s)", self.triage_manager.state)
        except (KeyError, AttributeError, TypeError) as _tm_err:
            logger.warning("[HyperSystem] TriageManager init failed: %s", _tm_err)

    # --------------------------------------------------------
    # Buy-fill callback (triage DCA 체결 확인)
    # --------------------------------------------------------
    def _on_buy_filled(
        self, *, ctx, market, strategy, entry_price, qty, funds, fee, reason,
    ):
        """매수 체결 시 트리아지 DCA 체결 확인."""
        reason = str(reason or "")
        if not reason.startswith("triage:"):
            return
        try:
            tm = getattr(self, "triage_manager", None)
            if tm and hasattr(tm, "on_dca_fill_confirmed"):
                tm.on_dca_fill_confirmed(
                    market=market,
                    entry_price=float(entry_price),
                    qty=float(qty),
                    funds=float(funds),
                    fee=float(fee),
                )
        except (KeyError, AttributeError, TypeError, ValueError) as e:
            logger.warning("[_on_buy_filled] triage DCA fill tracking error: %s", e)

    # --------------------------------------------------------
    # Sell-fill callback (autopilot + online calibrator)
    # --------------------------------------------------------
    def _on_sell_filled(
        self, *, ctx, market, strategy, pnl_pct,
        entry_price, exit_price, qty, hold_sec,
    ):
        strategy = str(strategy or "").strip().upper()
        if not strategy:
            return

        # 1) Autopilot loss tracking → Loss Cooldown gate
        try:
            mgr = getattr(self, "autopilot_manager", None)
            if mgr is not None:
                mgr.record_strategy_trade_result(strategy, is_win=(pnl_pct > 0))
        except (KeyError, AttributeError, TypeError):
            logger.warning("[_on_sell_filled] autopilot 거래결과 기록 실패: %s %s", market, strategy, exc_info=True)

        # 2) Online Calibrator → PP/AL parameter tuning
        if strategy in ("PINGPONG", "AUTOLOOP"):
            try:
                from app.manager.online_calibrator import get_calibrator
                cal = get_calibrator()
                atr_pct = 2.0
                regime = "RANGE"
                try:
                    sv = getattr(ctx, "strategy_vars", None) or {}
                    atr_pct = float(sv.get("atr_pct", 2.0) or 2.0)
                except (AttributeError, TypeError, ValueError):
                    logger.debug("[_on_sell_filled] atr_pct 추출 실패, 기본값 2.0 사용: %s", market)
                try:
                    regime, _ = self._infer_market_regime(ctx=ctx, price=exit_price)
                except (AttributeError, TypeError, ValueError):
                    logger.debug("[_on_sell_filled] 레짐 추론 실패, 기본값 RANGE 사용: %s", market)
                bucket = cal.classify_bucket(atr_pct, regime)
                tp_pct = 0.0
                sl_pct = 0.0
                try:
                    ss = getattr(ctx, "strategy_state", None) or {}
                    tp_pct = float(ss.get("tp_pct", 0) or 0)
                    sl_pct = float(ss.get("sl_pct", 0) or 0)
                except (AttributeError, TypeError, ValueError):
                    logger.debug("[_on_sell_filled] TP/SL 상태 추출 실패: %s", market)
                cal.record_trade(
                    bucket, strategy, pnl_pct,
                    tp_pct=tp_pct, sl_pct=sl_pct, hold_sec=hold_sec,
                )
            except (KeyError, AttributeError, TypeError, ValueError):
                logger.warning("[_on_sell_filled] 온라인 캘리브레이터 기록 실패: %s %s", market, strategy, exc_info=True)

        # 3) [2026-03-09] 매도 체결 즉시 슬롯 해제 + 짧은 쿨다운
        #    reconcile 30초 대기 없이 즉시 DISABLED 전환 → 슬롯 확보
        #    LADDER는 자체 관리이므로 제외
        _SELL_COOLDOWN_MIN = 3  # 매도 후 3분 재선택 방지 (과열 방지)
        if strategy not in ("LADDER",):
            try:
                # 즉시 DISABLED 전환
                self.oma_set_market(
                    market=market,
                    state=MarketState.DISABLED,
                    reason=[f"{strategy.lower()}_sell_completed"],
                )
                # 컨텍스트 정리
                try:
                    self.coordinator.remove_market(market)
                except (AttributeError, RuntimeError):
                    logger.warning("[_on_sell_filled] 컨텍스트 정리 실패: %s — 다음 매수 시 이전 상태 잔존 가능", market, exc_info=True)
                # [FIX 2026-03-23] LongHold config 자동 정리
                # 매도 완료된 코인이 longhold_config.json에 남아있으면
                # 슬롯 차감 + LONGHOLD_SELL_BLOCKED 반복 발생
                try:
                    _lm = getattr(self, "ladder_manager", None)
                    if _lm:
                        _lh_cfg = _lm.get_longhold_config(market)
                        if _lh_cfg and _lh_cfg.get("enabled"):
                            _lm.remove_longhold_config(market)
                            logger.info("[_on_sell_filled] LongHold config removed: %s", market)
                except (KeyError, AttributeError, TypeError):
                    logger.debug("[_on_sell_filled] longhold cleanup skip: %s", market, exc_info=True)
                # [FIX 2026-03-23] Ladder grid config 자동 비활성화
                # 매도 완료된 코인의 ladder_config가 enabled로 남아있으면
                # 유령 그리드가 슬롯을 점유하고 주문을 계속 시도
                try:
                    _lm2 = getattr(self, "ladder_manager", None)
                    if _lm2:
                        _ld_cfg = _lm2.get_config(market)
                        if isinstance(_ld_cfg, dict) and _ld_cfg.get("enabled"):
                            _ld_cfg["enabled"] = False
                            _lm2.save_config(_ld_cfg)
                            logger.info("[_on_sell_filled] Ladder config disabled: %s", market)
                except (KeyError, AttributeError, TypeError):
                    logger.debug("[_on_sell_filled] ladder cleanup skip: %s", market, exc_info=True)
                # 쿨다운 등록 (짧게 3분)
                self._autopilot_cooldown_mark(
                    market, minutes=_SELL_COOLDOWN_MIN,
                    reason=f"sell_filled:{strategy.lower()}"
                )
                # 원장 기록
                self.ledger.append(
                    "SLOT_FAST_RELEASE",
                    market=market,
                    strategy=strategy,
                    cooldown_min=_SELL_COOLDOWN_MIN,
                    pnl_pct=round(pnl_pct, 2),
                )
            except (KeyError, AttributeError, TypeError, ValueError):
                logger.warning("[_on_sell_filled] 슬롯 해제/원장 기록 실패: %s %s", market, strategy, exc_info=True)

        # 4) Immediate autopilot refill → 빈 슬롯 즉시 충원 (300초 대기 없이)
        try:
            import asyncio
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self.autopilot_step(reason="sell_refill"))
            except RuntimeError:
                logger.warning("[_on_sell_filled] no running event loop for autopilot refill", exc_info=True)
                main_loop = getattr(self, "_main_event_loop", None)
                if main_loop is not None and main_loop.is_running():
                    asyncio.run_coroutine_threadsafe(self.autopilot_step(reason="sell_refill"), main_loop)
        except (KeyError, AttributeError, TypeError):
            logger.warning("[_on_sell_filled] autopilot 즉시 재충원 실패: %s — 다음 주기에 자동 충원됨", market, exc_info=True)

    # --------------------------------------------------------
    # Lifespan hooks
    # --------------------------------------------------------
    async def start(self):
        import asyncio as _aio
        self._main_event_loop = _aio.get_running_loop()
        # 0) runtime/ stale tmp 파일 정리 (이전 크래시/비정상 종료 잔해)
        try:
            import glob as _glob
            runtime_dir = os.path.dirname(self.context_state_path) or "runtime"
            stale_tmps = _glob.glob(os.path.join(runtime_dir, "*.tmp*"))
            if stale_tmps:
                cleaned = 0
                for tmp in stale_tmps:
                    try:
                        os.remove(tmp)
                        cleaned += 1
                    except OSError as exc:
                        logger.warning("[BOOT] failed to clean stale tmp file in runtime/: %s", exc)
                if cleaned:
                    logger.info("[Boot] runtime/ stale tmp %d개 정리 완료", cleaned)
        except OSError as exc:
            logger.warning("[BOOT] runtime/ stale tmp cleanup scan failed: %s", exc, exc_info=True)

        # 1.2) [AUTO] Ladder 예산·계단수 자동 튜닝 → 백그라운드로 지연 (부팅 블로킹 방지)
        # 부팅 시 동기 실행하면 Bybit API 429 레이트리밋으로 30초+ 블로킹됨
        # tick_loop + price_feed + prewarm 등이 부팅 후 API를 많이 써서 60초 대기
        async def _deferred_ladder_tune():
            await asyncio.sleep(60.0)  # tick/price/prewarm 안정화 후 실행
            try:
                ladder_mgr = getattr(self, "ladder_manager", None)
                if not ladder_mgr:
                    return
                configs = ladder_mgr.list_configs()
                for cfg in configs:
                    market = cfg.get("market")
                    if not market:
                        continue
                    try:
                        result = await asyncio.get_running_loop().run_in_executor(
                            self._bg_executor, ladder_mgr.auto_tune_budget_and_levels, market
                        )
                        self.ledger.append("LADDER_AUTO_TUNE", market=market, result=result)
                    except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
                        logger.warning("[LADDER] deferred ladder auto-tune failed for market: %s", exc, exc_info=True)
                    await asyncio.sleep(3.0)  # 마켓 간 3초 간격 (429 방지)
            except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as e:
                self.ledger.append("LADDER_AUTO_TUNE_ERROR", error=str(e))
        asyncio.create_task(_deferred_ladder_tune())
        # 1) state 복원
        self.oma_registry.load()
        self._load_context_state()

        self.ledger.append(
            "SYSTEM_START",
            trading_mode=self.trading_mode,
            emergency_stop=self.emergency_stop,
            recovery_policy=self.recovery_policy,
        )

        # 1.5) [PATCH] Sync budget from context to registry if missing
        # 재부팅 시 OMA Registry에 예산 정보가 유실되었으나 Context에는 남아있는 경우(allocated_capital),
        # 이를 Registry로 복구하여 예산이 0으로 초기화되는 것을 방지한다.
        # [NEW] Registry -> Context 방향 동기화도 추가하여 상호 보완.
        try:
            # 1) Context -> Registry (Recover lost registry budget)
            for market, ctx in self.coordinator.contexts.items():
                # [FIX] Force sync allocated_capital to budget_usdt on boot to preserve allocation 100%
                # _load_context_state()에서 이미 포지션 기반 복구가 수행되었으므로,
                # 여기서는 ctx.allocated_capital을 믿고 OMA Registry에 고정 예산으로 박제합니다.
                if ctx.allocated_capital > 0:
                    self.oma_registry.set_state(
                        market=market,
                        state=self.oma_registry.get_state(market),
                        budget_usdt=ctx.allocated_capital,
                        persist=True
                    )
            
            # 2) Registry -> Context (Ensure context has budget immediately)
            # This helps if context state was stale/missing but registry has the truth.
            active_markets = self.oma_registry.list_active()
            for market in active_markets:
                reg_budget = self.oma_registry.get_budget_usdt(market)
                if reg_budget is not None and float(reg_budget) > 0:
                    ctx = self.coordinator.ensure_market(market)
                    if ctx.allocated_capital <= 0:
                        ctx.allocated_capital = float(reg_budget)
                        # If wallet mode, also init usable if empty
                        if getattr(self, 'wallet_mode', False) and ctx.usable_capital <= 0:
                            ctx.usable_capital = float(reg_budget)

        except (KeyError, AttributeError, TypeError, ValueError) as e:
            self.ledger.append("BUDGET_SYNC_ERROR", error=str(e))

        # 1.6) [PATCH] Sync strategy from context to OMA reason if missing
        # BTC(BTCUSDT)는 자동 배속하지 않되, 수동 전략 설정은 유지한다.
        try:
            for market in self.oma_registry.list_active():
                reasons = self.oma_registry.get_reason(market) or []
                has_strategy_tag = any(
                    isinstance(r, str) and r.upper().startswith("STRATEGY:")
                    for r in reasons
                )
                # BTC 전략 미지정 예외 처리
                if market.upper() == "BTCUSDT":
                    ctx = self.coordinator.contexts.get(market)
                    mode = ""
                    enabled = False
                    ctrls = None
                    if ctx:
                        ctrls = getattr(ctx, "controls", {}) or {}
                        sc = ctrls.get("strategy") or {}
                        if isinstance(sc, dict):
                            enabled = bool(sc.get("enabled"))
                            mode = str(sc.get("mode") or "").strip().upper()

                    # 수동 전략이 설정되어 있으면 유지 + reason 태그 보정
                    if enabled and mode and mode != "UNKNOWN":
                        if not has_strategy_tag:
                            new_reasons = list(reasons) + [f"strategy:{mode}"]
                            self.oma_registry.set_state(
                                market=market,
                                state=MarketState.ACTIVE,
                                reason=new_reasons,
                                persist=True
                            )
                            self.ledger.append("STRATEGY_REASON_SYNCED", market=market, strategy=mode)
                        continue

                    # 수동 전략이 없으면 자동 배속 방지: strategy mode/tag 제거
                    if ctx:
                        ctrls = ctrls or getattr(ctx, "controls", {}) or {}
                        if "strategy" in ctrls and isinstance(ctrls["strategy"], dict):
                            ctrls["strategy"].pop("mode", None)
                        ctx.controls = ctrls
                    new_reasons = [r for r in reasons if not (isinstance(r, str) and r.upper().startswith("STRATEGY:"))]
                    self.oma_registry.set_state(
                        market=market,
                        state=MarketState.ACTIVE,
                        reason=new_reasons,
                        persist=True
                    )
                    self.ledger.append("BTC_STRATEGY_UNASSIGNED", market=market)
                    continue
                if not has_strategy_tag:
                    # ctx에서 strategy 읽기
                    ctx = self.coordinator.contexts.get(market)
                    if ctx:
                        ctrls = getattr(ctx, "controls", {}) or {}
                        sc = ctrls.get("strategy") or {}
                        mode = str(sc.get("mode") or "").strip().upper()
                        if mode and mode != "UNKNOWN":
                            # reason에 strategy 태그 추가
                            new_reasons = list(reasons) + [f"strategy:{mode}"]
                            self.oma_registry.set_state(
                                market=market,
                                state=MarketState.ACTIVE,
                                reason=new_reasons,
                                persist=True
                            )
                            self.ledger.append("STRATEGY_REASON_SYNCED", market=market, strategy=mode)
        except (KeyError, AttributeError, TypeError, ValueError) as e:
            self.ledger.append("STRATEGY_REASON_SYNC_ERROR", error=str(e))

        # 2) known markets
        self._known_markets = set(self._load_known_markets())

        # 3) SNIPER 포지션 복구 — reconcile 전에 먼저 실행해야 orphan 오판(GAZUA 덮어쓰기) 방지
        try:
            from app.manager.sniper_position_store import sniper_store
            restored = sniper_store.restore_to_system(self)
            if restored > 0:
                self.ledger.append("SNIPER_RESTORED", count=restored)
        except (AttributeError, TypeError) as e:
            self.ledger.append("SNIPER_RESTORE_ERROR", error=str(e))

        # 4) boot reconcile
        try:
            self._last_reconcile_result = self.reconcile(reason="boot")
            self._last_reconcile_ts = time.time()
        except (OSError, TypeError, ValueError, OverflowError) as exc:
            self.ledger.append("RECONCILE_ERROR", error=str(exc), phase="boot")

        # 4-1) 부팅 후 검증 훅: position 있음 + strategy.mode 없음/혼합 → 자동 정합화
        try:
            self._boot_validate_positions()
        except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
            self.ledger.append("BOOT_VALIDATE_ERROR", error=str(exc))

        # 5) WATCH 자동 갱신
        if self._watch_task is None:
            self._watch_task = asyncio.create_task(self._watch_refresh_loop())

        # 5) PriceFeed 시작
        await self.price_feed.start()

        # 5.5) default executor 제한 적용
        asyncio.get_running_loop().set_default_executor(self._default_executor)

        # 5.6) PriceFeed 연결 후 재 reconcile (가격 데이터 확보 후 정확한 equity 계산)
        async def _deferred_reconcile():
            await asyncio.sleep(15.0)  # Bybit: WebSocket + API 안정화 대기
            try:
                self._last_reconcile_result = await asyncio.get_running_loop().run_in_executor(
                    self._bg_executor, lambda: self.reconcile(reason="boot_deferred")
                )
                self._last_reconcile_ts = time.time()
                logger.info("[Boot] deferred reconcile complete: cash=%.0f equity=%.0f",
                            self._last_cash_usdt, self._last_equity_usdt)
            except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
                logger.warning("[Boot] deferred reconcile failed: %s", exc)
        asyncio.create_task(_deferred_reconcile())

        # 6) Boot warm-up: 부팅 직후 모든 throttle 타이머를 now로 초기화하여
        #    reconcile/price_feed 안정화 전에 bg_executor 폭주(candle_loader 등) 방지
        _boot_now = time.time()
        self._last_rebalance_ts = _boot_now
        self._last_ladder_sync_ts = _boot_now
        self._last_portfolio_risk_ts = _boot_now
        self._last_smart_alert_ts = _boot_now
        self._ladder_tune_last_ts = _boot_now
        self._last_triage_poll_ts = _boot_now

        # 6) Tick loop 시작
        if self._tick_task is None:
            self._tick_task = asyncio.create_task(self._tick_loop())

        # 7) Autopilot loop (Reserved/OMA maintenance)
        # [2026-02-02] 재활성화 - AutopilotManager 대신 이 루프만 사용
        if self._autopilot_task is None:
            self._autopilot_task = asyncio.create_task(self._autopilot_loop())

        # 8) AI Auto-Retrain loop
        if self._ai_task is None:
            self._ai_task = asyncio.create_task(self._ai_loop())
        
        # 8.5) FOCUS strategy loop
        try:
            from app.manager.focus_manager import FocusManager
            if not hasattr(self, 'focus_manager'):
                self.focus_manager = FocusManager(system=self)
            if getattr(self, '_focus_task', None) is None:
                self._focus_task = asyncio.create_task(self._focus_loop())
        except Exception as exc:
            logger.warning("[BOOT] FOCUS manager init failed: %s", exc)

        # 8.6) Upbit FOCUS strategy loop (현물 long_only) — env opt-in, 기본 OFF.
        #      Bybit FOCUS 와 완전 격리. UPBIT_FOCUS_ENABLED 미설정 서버엔 0 영향.
        try:
            from app.core.constants import env_bool
            if env_bool("UPBIT_FOCUS_ENABLED", default=False):
                from app.manager.spot_gazua_manager import UpbitGazuaManager
                if not hasattr(self, 'upbit_gazua_manager'):
                    self.upbit_gazua_manager = UpbitGazuaManager(system=self)
                if getattr(self, '_upbit_gazua_task', None) is None:
                    self._upbit_gazua_task = asyncio.create_task(self._upbit_gazua_loop())
                logger.info("[BOOT] Upbit FOCUS manager initialized (paper=%s, enabled=%s)",
                            getattr(self.upbit_gazua_manager.config, 'paper', True),
                            getattr(self.upbit_gazua_manager.config, 'enabled', False))
        except Exception as exc:
            logger.warning("[BOOT] Upbit FOCUS manager init failed: %s", exc)

        # ── Bithumb 현물 FOCUS (Upbit 미러·완전 격리). BITHUMB_FOCUS_ENABLED 미설정 서버엔 0 영향. ──
        try:
            from app.core.constants import env_bool
            if env_bool("BITHUMB_FOCUS_ENABLED", default=False):
                from app.manager.bithumb_gazua_manager import BithumbGazuaManager
                if not hasattr(self, 'bithumb_gazua_manager'):
                    self.bithumb_gazua_manager = BithumbGazuaManager(system=self)
                if getattr(self, '_bithumb_gazua_task', None) is None:
                    self._bithumb_gazua_task = asyncio.create_task(self._bithumb_gazua_loop())
                logger.info("[BOOT] Bithumb FOCUS manager initialized (paper=%s, enabled=%s)",
                            getattr(self.bithumb_gazua_manager.config, 'paper', True),
                            getattr(self.bithumb_gazua_manager.config, 'enabled', False))
        except Exception as exc:
            logger.warning("[BOOT] Bithumb FOCUS manager init failed: %s", exc)

        # ── Bybit 현물 FOCUS (Upbit 미러·완전 격리). BYBIT_SPOT_FOCUS_ENABLED 미설정 서버엔 0 영향. ──
        try:
            from app.core.constants import env_bool
            if env_bool("BYBIT_SPOT_FOCUS_ENABLED", default=False):
                from app.manager.bybit_spot_gazua_manager import BybitSpotGazuaManager
                if not hasattr(self, 'bybit_spot_gazua_manager'):
                    self.bybit_spot_gazua_manager = BybitSpotGazuaManager(system=self)
                if getattr(self, '_bybit_spot_gazua_task', None) is None:
                    self._bybit_spot_gazua_task = asyncio.create_task(self._bybit_spot_gazua_loop())
                logger.info("[BOOT] Bybit SPOT FOCUS manager initialized (paper=%s, enabled=%s)",
                            getattr(self.bybit_spot_gazua_manager.config, 'paper', True),
                            getattr(self.bybit_spot_gazua_manager.config, 'enabled', False))
        except Exception as exc:
            logger.warning("[BOOT] Bybit SPOT FOCUS manager init failed: %s", exc)

        # ── Binance 현물 FOCUS (Upbit 미러·완전 격리). BINANCE_SPOT_FOCUS_ENABLED 미설정 서버엔 0 영향. ──
        try:
            from app.core.constants import env_bool
            if env_bool("BINANCE_SPOT_FOCUS_ENABLED", default=False):
                from app.manager.binance_spot_gazua_manager import BinanceSpotGazuaManager
                if not hasattr(self, 'binance_spot_gazua_manager'):
                    self.binance_spot_gazua_manager = BinanceSpotGazuaManager(system=self)
                if getattr(self, '_binance_spot_gazua_task', None) is None:
                    self._binance_spot_gazua_task = asyncio.create_task(self._binance_spot_gazua_loop())
                logger.info("[BOOT] Binance SPOT FOCUS manager initialized (paper=%s, enabled=%s)",
                            getattr(self.binance_spot_gazua_manager.config, 'paper', True),
                            getattr(self.binance_spot_gazua_manager.config, 'enabled', False))
        except Exception as exc:
            logger.warning("[BOOT] Binance SPOT FOCUS manager init failed: %s", exc)

        # ── Binance USDT-M 선물 FOCUS (Bybit FOCUS 미러·완전 격리). BINANCE_FUTURES_ENABLED 미설정 서버엔 0 영향. ──
        try:
            from app.core.constants import env_bool
            if env_bool("BINANCE_FUTURES_ENABLED", default=False):
                from app.manager.binance_futures_manager import BinanceFuturesManager
                if not hasattr(self, 'binance_futures_manager'):
                    self.binance_futures_manager = BinanceFuturesManager(system=self)
                if getattr(self, '_binance_futures_task', None) is None:
                    self._binance_futures_task = asyncio.create_task(self._binance_futures_loop())
                logger.info("[BOOT] Binance FUTURES FOCUS manager initialized (force_paper=%s, enabled=%s)",
                            getattr(self.binance_futures_manager, '_force_paper', True),
                            getattr(self.binance_futures_manager.config, 'enabled', False))
        except Exception as exc:
            logger.warning("[BOOT] Binance FUTURES FOCUS manager init failed: %s", exc)

        # 9) Contrarian auto-scan loop
        if getattr(self, '_contrarian_task', None) is None:
            self._contrarian_task = asyncio.create_task(self._contrarian_loop())

        # 10) 일일 리포트 발송 루프
        if getattr(self, '_daily_report_task', None) is None:
            self._daily_report_task = asyncio.create_task(self._daily_report_loop())

        # 11) 전략별 추천코인 백그라운드 직렬 pre-warm (SLOW_TICK 방지)
        if getattr(self, '_recommend_task', None) is None:
            self._recommend_task = asyncio.create_task(self._strategy_recommend_loop())

        # 11-b) Watchlist subscribe loop — sync top markets to WebSocket feed
        if getattr(self, '_watchlist_subscribe_task', None) is None:
            self._watchlist_subscribe_task = asyncio.create_task(self._watchlist_subscribe_loop())

        # 12) Volume Spike Detector 초기화 + 주기적 업데이트
        if getattr(self, '_volume_spike_task', None) is None:
            try:
                from app.monitor.volume_spike_detector import initialize_volume_spike_detector
                from app.integrations.bybit_markets import (
                    fetch_bybit_markets as _vs_fetch_markets,
                    filter_quote_markets as _vs_filter_markets,
                )
                import requests as _vs_requests

                class _VolumeSpikeBybitClient:
                    """Volume Spike Detector용 일봉 API 클라이언트 (Bybit)"""
                    @staticmethod
                    def get_candles_daily(market: str, count: int = 7):
                        try:
                            # Bybit kline API for daily candles
                            from app.integrations.bybit_markets import fetch_bybit_kline
                            return fetch_bybit_kline(market, interval="D", limit=count)
                        except (ImportError, AttributeError, TypeError) as exc:
                            logger.warning("[BYBIT] kline API fetch for daily candles failed: %s", exc, exc_info=True)
                        return []

                initialize_volume_spike_detector(_VolumeSpikeBybitClient())
                self._volume_spike_task = asyncio.create_task(self._volume_spike_update_loop())
                self.ledger.append("VOLUME_SPIKE_INIT", ok=True)
            except (AttributeError, TypeError) as e:
                logger.warning("[VolumeSpikeDetector] init failed: %s", e)

        # 13) Cross Exchange Monitor 시작
        if getattr(self, '_cross_exchange_task', None) is None:
            try:
                from app.monitor.cross_exchange_monitor import CrossExchangeMonitor
                _cx_enabled = _env_bool("OMA_CROSS_EXCHANGE_ENABLED", True)
                if _cx_enabled:
                    self._cx_monitor = CrossExchangeMonitor(use_mock=True)
                    await self._cx_monitor.initialize()
                    self._cross_exchange_task = asyncio.create_task(
                        self._cx_monitor.start_monitoring()
                    )
                    self.ledger.append("CROSS_EXCHANGE_INIT", ok=True)
            except (AttributeError, TypeError) as e:
                logger.warning("[CrossExchangeMonitor] init failed: %s", e)

    # [5C] _boot_validate_positions → hs_mixin_reconcile.py

    async def stop(self):
        # state 저장
        self._save_context_state()
        self.oma_registry.save()
        self.ledger.append("SYSTEM_STOP")

        if self._watch_task is not None:
            self._watch_task.cancel()
            self._watch_task = None

        if self._tick_task is not None:
            self._tick_task.cancel()
            self._tick_task = None

        if self._autopilot_task is not None:
            self._autopilot_task.cancel()
            self._autopilot_task = None

        if self._ai_task is not None:
            self._ai_task.cancel()
            self._ai_task = None
        
        if getattr(self, '_contrarian_task', None) is not None:
            self._contrarian_task.cancel()
            self._contrarian_task = None

        if getattr(self, '_daily_report_task', None) is not None:
            self._daily_report_task.cancel()
            self._daily_report_task = None

        if getattr(self, '_volume_spike_task', None) is not None:
            self._volume_spike_task.cancel()
            self._volume_spike_task = None

        if getattr(self, '_cross_exchange_task', None) is not None:
            self._cross_exchange_task.cancel()
            self._cross_exchange_task = None

        await self.price_feed.stop()

    # [5A] _load_sector_map_from_file → hs_mixin_state_io.py

    # [5A] _load_context_state → hs_mixin_state_io.py















    # [5A] _CONTEXT_STATE_MAX_BYTES, _save_context_state → hs_mixin_state_io.py

    # [5B] UI settings methods → hs_mixin_ui_settings.py

    # [5A] _save_ui_settings → hs_mixin_state_io.py

    # [5A] persist_ui_settings → hs_mixin_state_io.py

    # [5A] _load_autopilot_cooldown, _save_autopilot_cooldown, _autopilot_cooldown_prune, get_autopilot_cooldown_markets → hs_mixin_state_io.py

    def effective_min_order_usdt(self) -> float:
        """대시보드/ENV `min_order_usdt` (USDT). 미설정·비정상 시 거래소 기본 `Q.config.min_order`."""
        try:
            v = float(self.min_order_usdt)
        except (TypeError, ValueError):
            v = 0.0
        if v > 0.0:
            return v
        return float(Q.min_order)

    def is_night_mode_active(self, ts: Optional[float] = None) -> bool:
        """Night Mode 활성 여부 판단 (시간대 기반)."""
        if not getattr(self, 'night_mode_enabled', False):
            return False
        lt = time.localtime(ts or time.time())
        h = lt.tm_hour
        start = int(getattr(self, 'night_mode_start_hour', 2))
        end = int(getattr(self, 'night_mode_end_hour', 9))
        if start <= end:
            return start <= h < end
        else:
            # 자정 넘는 경우 (예: 22~9)
            return h >= start or h < end

    def get_night_mode_config(self) -> Dict[str, Any]:
        """Night Mode 전체 설정 반환 (API/대시보드용)."""
        return {
            "enabled": bool(getattr(self, 'night_mode_enabled', False)),
            "active_now": self.is_night_mode_active(),
            "start_hour": int(getattr(self, 'night_mode_start_hour', 2)),
            "end_hour": int(getattr(self, 'night_mode_end_hour', 9)),
            "entry_score_boost_pct": float(getattr(self, 'night_mode_entry_score_boost_pct', 30.0)),
            "sl_multiplier": float(getattr(self, 'night_mode_sl_multiplier', 1.5)),
        }

    # [5A] _autopilot_cooldown_mark → hs_mixin_state_io.py

    # --------------------------------------------------------
    # OMA control
    # --------------------------------------------------------
    def oma_set_market(self, market: str, state: MarketState, reason: list[str] | None = None, budget_usdt: float | None = None):
        """OMA registry state update + coordinator sync.

        목적
        - OMA 코인리스트(Registry)와 Coordinator/대시보드(PnL)의 즉시 동기화
        - 포지션/주문이 남아있는 상태에서 DISABLED/WATCH로 내리는 위험 동작 방지
        - 비관리 코인 컨텍스트를 정리해 PnL '좀비' 행을 제거
        """
        with self._lock:
            market = str(market or '').strip()
            reason_list = list(reason or [])

            # 현재 상태(전이 판단용)
            prev_state = self.oma_registry.get_state(market)

            # 컨텍스트가 존재하면, 보유/주문 여부를 확인한다
            ctx_existing = None
            try:
                ctx_existing = self.coordinator.contexts.get(market)
            except (AttributeError, TypeError):
                logger.warning("[set_market_state] failed to get existing context for %s", market, exc_info=True)
                ctx_existing = None

            has_open_pos = bool(getattr(ctx_existing, 'position', None)) if ctx_existing else False
            has_open_order = bool(getattr(ctx_existing, 'order_state', None)) if ctx_existing else False

            # [PATCH] Preserve existing budget if not provided
            # This prevents accidental budget reset when changing state via generic admin panel
            if budget_usdt is None:
                try:
                    existing_budget = self.oma_registry.get_budget_usdt(market)
                    if existing_budget is not None:
                        budget_usdt = float(existing_budget)
                except (AttributeError, TypeError, ValueError) as exc:
                    logger.error("[BUDGET] failed to preserve existing budget during config change: %s", exc, exc_info=True)

            # 안전장치: 포지션/주문이 남아있는데 DISABLED/WATCH 요청 → RECOVERY로 자동 승격
            # 단, dust 정리로 인한 DISABLED는 재승격 차단 (_dust_disabled 플래그)
            _is_dust_disabled = bool(getattr(ctx_existing, '_dust_disabled', False)) if ctx_existing else False
            if state in (MarketState.DISABLED, MarketState.WATCH) and (has_open_pos or has_open_order) and not _is_dust_disabled:
                state = MarketState.RECOVERY
                reason_list = list(reason_list) + ['auto_promote_recovery_on_disable_with_position']

            # 1) Registry 기록 (runtime/oma_state.json)
            self.oma_registry.set_state(market=market, state=state, reason=reason_list, budget_usdt=budget_usdt)

            # 2) Coordinator/PriceFeed 동기화
            if state in (MarketState.ACTIVE, MarketState.RECOVERY):
                # 관리 대상은 컨텍스트를 보장
                self.coordinator.activate_market(market)
                ctx = self.coordinator.get_context(market)

                # [2026-02-02] ACTIVE/RECOVERY 전환 시 워밍업 강제 완료
                try:
                    ctx.force_ready()
                except (AttributeError, TypeError, RuntimeError) as exc:
                    try:
                        self.ledger.append("FORCE_READY_ERROR", market=market, error=str(exc))
                    except (AttributeError, TypeError, ValueError) as exc:
                        logger.warning("[MARKET] ledger append for force-ready error failed: %s", exc)

                # UI 가시성을 위한 상태 동기화
                try:
                    ctx.market_state = str(state.value)
                except (AttributeError, TypeError, ValueError):
                    logger.warning("[set_market_state] failed to set market_state via .value for %s", market, exc_info=True)
                    ctx.market_state = str(state)

                # RECOVERY: 진입 금지 + 회수 관리 플래그
                if state == MarketState.RECOVERY:
                    ctx.recovery = True
                    ctx.recovery_reason = list(reason_list)
                    ctx.recovery_since_ts = time.time()
                else:
                    ctx.recovery = False
                    ctx.recovery_reason = None
                    ctx.recovery_since_ts = None

                # 관측용 타임스탬프
                try:
                    ctx.engine_started_ts = time.time()
                except (AttributeError, TypeError, ValueError) as exc:
                    logger.warning("[MARKET] engine_started_ts timestamp set failed: %s", exc)

                # ACTIVE/RECOVERY → pricefeed가 즉시 재구독해야 warm-up이 즉시 시작됨
                try:
                    self.price_feed.request_resubscribe()
                except (AttributeError, RuntimeError) as exc:
                    logger.warning("[MARKET] price feed resubscribe request failed after activation: %s", exc, exc_info=True)

            else:
                # WATCH/DISABLED: 컨텍스트를 즉시 정리해서 PnL/상태가 바로 반영되게 한다
                ctx = None
                try:
                    ctx = self.coordinator.contexts.get(market)
                except (AttributeError, TypeError):
                    logger.warning("[set_market_state] WATCH/DISABLED: failed to get context for %s", market, exc_info=True)
                    ctx = None

                if ctx is not None:
                    try:
                        ctx.market_state = str(state.value)
                    except (AttributeError, TypeError, ValueError):
                        logger.warning("[set_market_state] WATCH/DISABLED: failed to set market_state via .value for %s", market, exc_info=True)
                        ctx.market_state = str(state)

                    # 관리 플래그 정리
                    ctx.recovery = False
                    ctx.recovery_reason = None
                    ctx.recovery_since_ts = None

                    # 배당/지갑 상한을 0으로 리셋(비관리 코인이 PnL에 남는 현상 방지)
                    try:
                        ctx.allocated_capital = 0.0
                    except (AttributeError, TypeError, ValueError) as exc:
                        try:
                            self.ledger.append("LONGHOLD_SCHEDULE_ERROR", error=str(exc))
                        except (AttributeError, TypeError, ValueError) as exc:
                            logger.warning("[LONGHOLD] ledger append for schedule reset error failed: %s", exc)
                    try:
                        if getattr(self, 'wallet_mode', False):
                            ctx.usable_capital = 0.0
                    except (AttributeError, TypeError, ValueError) as exc:
                        try:
                            self.ledger.append("LONGHOLD_SCHEDULE_ERROR", error=str(exc))
                        except (AttributeError, TypeError, ValueError) as exc:
                            logger.warning("[LONGHOLD] ledger append for schedule reset error failed: %s", exc)

                    # 재활성화 시 즉시 READY 되는 것을 방지
                    try:
                        ctx.reset_warmup()
                    except (AttributeError, TypeError, RuntimeError) as exc:
                        try:
                            self.ledger.append("LONGHOLD_SCHEDULE_ERROR", error=str(exc))
                        except (AttributeError, TypeError, ValueError) as exc:
                            logger.warning("[LONGHOLD] ledger append for cooldown reset error failed: %s", exc)

                    # 포지션/주문이 없으면 컨텍스트 자체를 제거(= 즉시 '좀비 행' 제거)
                    # 단, UI에서 전략/가드(안전장치) 오버라이드를 걸어둔 경우에는
                    # 재시작 후에도 설정이 유지되도록 컨텍스트를 보존한다.
                    keep_ctx = False
                    try:
                        controls = getattr(ctx, 'controls', {}) or {}
                        if isinstance(controls, dict):
                            sc = controls.get('strategy', {}) or {}
                            if isinstance(sc, dict) and bool(sc.get('enabled')):
                                keep_ctx = True

                            gc = controls.get('guards') or {}
                            if isinstance(gc, dict) and len(gc.keys()) > 0:
                                keep_ctx = True
                    except (AttributeError, TypeError, ValueError):
                        logger.warning("[set_market_state] failed to check controls for keep_ctx on %s", market, exc_info=True)
                        keep_ctx = False

                    if (not keep_ctx) and (not getattr(ctx, 'position', None)) and (not getattr(ctx, 'order_state', None)):
                        try:
                            self.coordinator.remove_market(market)
                        except (AttributeError, RuntimeError) as exc:
                            logger.warning("[MARKET] coordinator remove_market failed during deactivation: %s", exc, exc_info=True)

                # 구독 목록이 바뀌었을 수 있으므로 재구독 요청(반영 지연 최소화)
                try:
                    self.price_feed.request_resubscribe()
                except (AttributeError, RuntimeError) as exc:
                    logger.warning("[MARKET] price feed resubscribe after market list change failed: %s", exc, exc_info=True)

            # 3) Allocation 즉시 갱신(다음 tick을 기다리지 않고 PnL/배당 반영)
            try:
                self._rebalance_allocations(active_markets=list(self.oma_registry.list_active()))
            except (KeyError, AttributeError, TypeError) as exc:
                logger.error("[BUDGET] immediate allocation rebalance after market change failed: %s", exc, exc_info=True)

            # 4) context_state.json 즉시 flush (재부팅 시 '좀비 복원' 방지)
            try:
                self._save_context_state()
            except (OSError, TypeError, ValueError) as exc:
                logger.warning("[STATE] context_state.json flush after market change failed: %s", exc, exc_info=True)

    def purge_market(self, market: str, *, reason: str = "user_purge"):
        with self._lock:
            market = str(market).strip()
            if not market:
                return {"ok": False, "error": "empty_market"}

            # 1) OMA Registry 완전 제거 (DISABLED + 내부 dict 제거)
            try:
                if self.oma_registry.has_market(market):
                    self.oma_registry._markets.pop(market, None)
                    self.oma_registry.save()
            except (KeyError, AttributeError, TypeError) as exc:
                logger.warning("[CLEANUP] OMA registry full removal failed for market: %s", exc, exc_info=True)

            # 2) Coordinator context 제거
            try:
                self.coordinator.remove_market(market)
            except (AttributeError, RuntimeError) as exc:
                logger.warning("[CLEANUP] coordinator context removal failed for market: %s", exc, exc_info=True)

            # 3) PriceStore 제거 (좀비 PnL/평가 방지)
            try:
                price_store._prices.pop(market, None)
            except (KeyError, AttributeError, TypeError) as exc:
                logger.warning("[CLEANUP] price store removal failed for market: %s", exc, exc_info=True)

            # 4) ProfitStore 제거 (실현 손익 캐시)
            try:
                from app.manager.profit_store import profit_store
                profit_store.trades.pop(market, None)
            except (KeyError, AttributeError, TypeError) as exc:
                logger.warning("[CLEANUP] profit store removal failed for market: %s", exc, exc_info=True)

            # 5) Ladder 설정/주문 제거
            try:
                if self.ladder_manager:
                    self.ladder_manager.purge_market(market)
            except (AttributeError, TypeError, RuntimeError) as exc:
                logger.warning("[CLEANUP] ladder settings/orders removal failed for market: %s", exc, exc_info=True)

            # 6) context_state.json 즉시 저장 (재부팅 좀비 방지)
            try:
                self._save_context_state()
            except (OSError, TypeError, ValueError) as exc:
                logger.warning("[STATE] context_state.json save after cleanup failed: %s", exc, exc_info=True)

            # 7) PriceFeed 재구독
            try:
                self.price_feed.request_resubscribe()
            except (AttributeError, RuntimeError) as exc:
                logger.warning("[CLEANUP] price feed resubscribe after market removal failed: %s", exc, exc_info=True)

            # 8) 로그
            self.ledger.append("MARKET_PURGED", market=market, reason=reason)

            return {"ok": True, "market": market}

    # --------------------------------------------------------
    # WATCH auto refresh
    # --------------------------------------------------------

    def oma_refresh_watch(self) -> int:
        markets = fetch_bybit_markets()
        # Keep app/data/bybit_markets.json refreshed (for UI/config usage)
        try:
            min_interval = float(os.environ.get('BYBIT_MARKETS_CACHE_MIN_INTERVAL_SEC', '3600'))
            ensure_bybit_markets_cache(markets, min_interval_sec=min_interval, quote="USDT")
        except (KeyError, AttributeError, TypeError, ValueError) as exc:
            # Never fail the watch refresh loop due to cache I/O errors
            logger.warning("[BYBIT] bybit_markets.json cache refresh failed: %s", exc, exc_info=True)
        usdt_markets = filter_quote_markets(markets)

        def _is_user_disabled(reasons: list[str] | None) -> bool:
            for r in (reasons or []):
                s = str(r).lower()
                if ("user_disabled" in s) or ("delete" in s) or ("stop_ui" in s) or ("stop_btn" in s):
                    return True
            return False

        added = 0
        for market in usdt_markets:
            if self.oma_registry.get_state(market) == MarketState.DISABLED:
                try:
                    reasons = self.oma_registry.get_reason(market)
                except (AttributeError, TypeError):
                    logger.warning("[_ensure_bybit_usdt_markets] failed to get reason for %s", market, exc_info=True)
                    reasons = []
                if _is_user_disabled(reasons):
                    continue
                self.oma_registry.set_state(
                    market=market,
                    state=MarketState.WATCH,
                    reason=["Bybit USDT Market"],
                )
                added += 1

        self.watch_last_refresh_ts = time.time()
        return added

    async def _watch_refresh_loop(self):
        try:
            self.oma_refresh_watch()
        except (AttributeError, TypeError, ValueError) as exc:
            logger.warning("[WATCH] oma_refresh_watch failed in watch refresh loop: %s", exc, exc_info=True)

        while True:
            await asyncio.sleep(self.watch_refresh_interval_sec)
            try:
                self.oma_refresh_watch()
            except (AttributeError, TypeError, ValueError) as exc:
                logger.warning("[WATCH] oma_refresh_watch retry failed in watch refresh loop: %s", exc, exc_info=True)

    # --------------------------------------------------------
    # Reconcile (boot/periodic/manual)
    # --------------------------------------------------------
    def _load_known_markets(self) -> List[str]:
        try:
            local_path = os.path.join("app", "data", "bybit_markets.json")
            if os.path.exists(local_path):
                with open(local_path, "r", encoding="utf-8") as f:
                    data = json.load(f)

                items = None
                if isinstance(data, list):
                    items = data
                elif isinstance(data, dict):
                    # 신규 포맷
                    if "items" in data and isinstance(data["items"], list):
                        items = data["items"]
                    # 구 legacy 포맷 대응
                    elif "markets" in data and isinstance(data["markets"], list):
                        items = data["markets"]

                if items:
                    out = []
                    for it in items:
                        if isinstance(it, dict) and it.get("market"):
                            out.append(str(it["market"]))
                    return out
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            logger.warning("[BOOT] known_markets legacy format parse failed: %s", exc, exc_info=True)

        # fallback
        try:
            mk = fetch_bybit_markets()
            return list(filter_quote_markets(mk))
        except (KeyError, AttributeError, TypeError) as exc:
            try:
                self.ledger.append("KNOWN_MARKETS_LOAD_ERROR", error=str(exc))
            except (AttributeError, TypeError, ValueError) as exc:
                logger.warning("[BOOT] known_markets load error ledger append failed: %s", exc)
            return []


    # [5C] reconcile/recovery/dust methods → hs_mixin_reconcile.py

    # --------------------------------------------------------
    # Tick loop
    # --------------------------------------------------------
    

    def _tick_ladder_grid_sync(self) -> None:
        """ICAG grid sync: LADDER 마켓 중 grid_auto_sync=True인 마켓만 sync.
        
        [2026-03-10] LADDER controls 자동 복구는 엔진 상태와 무관하게 실행.
        실제 그리드 sync만 엔진 active 시 실행.
        """
        if bool(getattr(self, "emergency_stop", False)):
            return

        now = time.time()
        last = getattr(self, "_last_grid_sync_ts", 0.0)
        if (now - last) < 10:
            return
        self._last_grid_sync_ts = now

        # ── Phase 1: LADDER controls 자동 복구 (엔진 상태 무관) ──
        # OMA에 strategy:LADDER로 ACTIVE인데 controls.strategy.mode가 LADDER가 아니면
        # apply_engine_controls 호출하여 controls + ladder_config.json 생성
        for market in self.oma_registry.list_active():
            ctx = self.coordinator.contexts.get(market)
            if ctx is None:
                continue
            ctrl = getattr(ctx, "controls", None) or {}
            strat = ctrl.get("strategy", {}) if isinstance(ctrl, dict) else {}
            mode_upper = str(strat.get("mode", "")).upper()

            if mode_upper != "LADDER":
                try:
                    reasons = self.oma_registry.get_reason(market) or []
                    is_ladder_market = any("LADDER" in str(r).upper() for r in reasons)
                    if is_ladder_market:
                        from app.manager.market_controls import apply_engine_controls
                        apply_engine_controls(self, market, "LADDER")
                        logger.info("[LADDER GridSync] auto-bootstrapped controls for %s", market)
                except (AttributeError, TypeError, ValueError) as exc:
                    logger.warning("[LADDER GridSync] bootstrap %s failed: %s", market, exc)

        # ── Phase 2: 실제 그리드 sync (엔진 active 필요) ──
        engine = getattr(getattr(self, "coordinator", None), "engine", None)
        status = getattr(engine, "status", None)
        if not bool(getattr(status, "is_active", False)):
            return

        # ICAG v3 engine (lazy init)
        grid_v3 = getattr(self, "_ladder_grid_v3", None)
        if grid_v3 is None:
            mgr = getattr(self, "ladder_manager", None)
            if mgr is None:
                return
            try:
                from app.manager.ladder_grid_v3 import LadderGridV3
                grid_v3 = LadderGridV3(mgr)
                self._ladder_grid_v3 = grid_v3
            except (ImportError, AttributeError, TypeError) as e:
                logger.error("[LADDER] ICAG GridV3 initialization failed: %s", e, exc_info=True)
                self._ladder_grid_v3 = False
                return
        if grid_v3 is False:
            return

        for market in self.oma_registry.list_active():
            ctx = self.coordinator.contexts.get(market)
            if ctx is None:
                continue

            ctrl = getattr(ctx, "controls", None) or {}
            strat = ctrl.get("strategy", {}) if isinstance(ctrl, dict) else {}
            mode_upper = str(strat.get("mode", "")).upper()

            if not bool(strat.get("enabled", False)):
                continue
            if mode_upper != "LADDER":
                continue
            params = strat.get("params", {}) if isinstance(strat, dict) else {}
            if not params.get("grid_auto_sync", False):
                continue

            try:
                out = grid_v3.poll_and_sync(market)
                fills = out.get("fills", []) if isinstance(out, dict) else []
                if fills:
                    logger.info("ICAG poll_filled %s: %d events", market, len(fills))
                result = out.get("sync", {}) if isinstance(out, dict) else {}
                if result and not result.get("skipped"):
                    logger.info(
                        "ICAG sync %s: anchor=%.2f zone=%s bias=%s step=%.4f buy=%d sell=%d",
                        market,
                        result.get("anchor", 0),
                        result.get("zone", "?"),
                        result.get("bias", "?"),
                        result.get("step", 0),
                        result.get("placed_buy", 0),
                        result.get("placed_sell", 0),
                    )
                elif result and result.get("skipped"):
                    logger.debug("ICAG sync %s skipped: %s", market, result.get("reason", "?"))
            except (KeyError, AttributeError, TypeError) as e:
                logger.warning("ICAG sync %s error: %s", market, e)

    async def _run_longhold_poll(self) -> None:
        """Run LongHold poll in a background thread to avoid blocking tick loop."""
        if not bool(getattr(self, "longhold_alerts_enabled", False)):
            return

        mgr = getattr(self, "ladder_manager", None)
        if mgr is None or not hasattr(mgr, "poll_longhold_alerts"):
            return

        if self._longhold_poll_lock.locked():
            return

        async with self._longhold_poll_lock:
            try:
                # poll_longhold_alerts is synchronous (may do network I/O)
                await asyncio.to_thread(mgr.poll_longhold_alerts)
            except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
                try:
                    self.ledger.append("LONGHOLD_POLL_ERROR", error=str(exc))
                except (AttributeError, TypeError, ValueError):
                    logger.warning("[LongHold] poll_longhold_alerts 원장 기록도 실패: %s", exc)

    async def _check_profit_lock_tick(self, market: str, price: float, ctx) -> None:
        """[④] 수익 자동 락인: 코인이 trigger_pct% 수익 도달 시 sell_ratio만큼 부분매도.

        global_profit_take(전량 매도)와 달리 sell_ratio(기본 30%)만 매도하고 나머지 홀드.
        market별 쿨다운(기본 1시간)으로 중복 발동 방지. 재시작 시 쿨다운 초기화(허용).
        """
        if not self.profit_lock_enabled:
            return
        try:
            pos = getattr(ctx, "position", None)
            if not pos:
                return
            entry = float(pos.get("entry", 0.0) or 0.0)
            qty = float(pos.get("qty", 0.0) or 0.0)
            if entry <= 0 or qty <= 0 or price <= 0:
                return
            pnl_pct = (price / entry - 1.0) * 100.0
            if pnl_pct < self.profit_lock_trigger_pct:
                return
            # market별 쿨다운 체크
            lock_key = f"_profit_lock_ts_{market.replace('-', '_')}"
            last_ts = float(getattr(self, lock_key, 0.0) or 0.0)
            if (time.time() - last_ts) < self.profit_lock_cooldown_sec:
                return
            # [FIX 2026-03-23] 타임스탬프는 _handle_intent() 제출 성공 후 찍음
            # 이전: setattr 먼저 → order 차단/예외 시 1시간 쿨다운 낭비
            sell_qty = qty * self.profit_lock_sell_ratio
            sell_value = sell_qty * price
            remain_value = (qty - sell_qty) * price
            # 부분매도 금액이 최소 주문금액 미달 또는 잔량이 dust → 전량 매도로 전환
            partial = True
            if sell_value < self.min_order_usdt or remain_value < self.min_order_usdt:
                sell_qty = qty
                partial = False
            self.ledger.append(
                "PROFIT_LOCK_TRIGGERED", market=market,
                pnl_pct=round(pnl_pct, 2),
                trigger_pct=self.profit_lock_trigger_pct,
                sell_ratio=self.profit_lock_sell_ratio if partial else 1.0,
                sell_qty=sell_qty,
                full_sell_fallback=not partial,
            )
            await self._handle_intent(
                market=market, price=price, ctx=ctx,
                intent={
                    "action": "sell",
                    "sell_qty": sell_qty,
                    "reason": f"profit_lock:+{pnl_pct:.1f}%{'(full)' if not partial else ''}",
                    "partial_sell": partial,
                    "meta": {"exit_kind": "profit_lock", "pnl_pct": pnl_pct},
                },
            )
            setattr(self, lock_key, time.time())
        except (OSError, KeyError, AttributeError, TypeError, ValueError, OverflowError) as exc:
            logger.error("[SELL] partial sell amount below minimum or dust qty - switching to full sell: %s", exc, exc_info=True)

    async def _check_peak_drawdown_tick(self, market: str, price: float, ctx) -> None:
        """[2026-03-24] Peak Drawdown Guard: TP 근접 후 급락 시 자동 매도.

        - 포지션의 최고 수익률(peak)을 strategy_vars에 추적
        - peak >= TP × activation_pct 도달 이력이 있고
        - 현재 수익이 peak × (trigger_pct/100) 이하로 떨어지면 전량 매도
        - 최소 수익률(min_profit_pct) 이상일 때만 발동 (손실 매도 방지)
        """
        if not self.peak_drawdown_guard_enabled:
            return
        try:
            pos = getattr(ctx, "position", None)
            if not pos:
                return
            entry = float(pos.get("entry", 0.0) or 0.0)
            qty = float(pos.get("qty", 0.0) or 0.0)
            if entry <= 0 or qty <= 0 or price <= 0:
                return
            pnl_pct = (price / entry - 1.0) * 100.0

            # strategy_vars에 peak 추적
            svars = getattr(ctx, "strategy_vars", None)
            if svars is None:
                svars = {}
                ctx.strategy_vars = svars
            peak_key = "_pdg_peak_pnl_pct"
            activated_key = "_pdg_activated"
            prev_peak = float(svars.get(peak_key, 0.0) or 0.0)

            # 현재 수익이 이전 peak보다 높으면 갱신
            if pnl_pct > prev_peak:
                svars[peak_key] = pnl_pct
                prev_peak = pnl_pct

            # 수익이 0 이하이면 리셋 (손실 구간 — peak 추적 무의미)
            if pnl_pct <= 0:
                svars[peak_key] = 0.0
                svars[activated_key] = False
                return

            # TP 조회: 전략별 정책 TP
            tp_pct = self._get_effective_tp_for_market(ctx)
            if tp_pct <= 0:
                return

            # activation 체크: peak이 TP의 activation_pct% 이상 도달했는가
            activation_threshold = tp_pct * (self.peak_drawdown_activation_pct / 100.0)
            was_activated = bool(svars.get(activated_key, False))
            if prev_peak >= activation_threshold:
                if not was_activated:
                    svars[activated_key] = True
                    was_activated = True

            if not was_activated:
                return

            # trigger 체크: 현재 수익이 peak의 trigger_pct% 이하로 떨어졌는가
            trigger_floor = prev_peak * (self.peak_drawdown_trigger_pct / 100.0)
            if pnl_pct > trigger_floor:
                return  # 아직 충분히 빠지지 않음

            # 최소 수익률 가드: 손실 매도 방지
            if pnl_pct < self.peak_drawdown_min_profit_pct:
                return

            # 매도 실행
            self.ledger.append(
                "PEAK_DRAWDOWN_SELL", market=market,
                peak_pct=round(prev_peak, 3),
                current_pct=round(pnl_pct, 3),
                tp_pct=round(tp_pct, 3),
                activation_threshold=round(activation_threshold, 3),
                trigger_floor=round(trigger_floor, 3),
            )
            await self._handle_intent(
                market=market, price=price, ctx=ctx,
                intent={
                    "action": "sell",
                    "reason": f"peak_drawdown:peak={prev_peak:.1f}%→now={pnl_pct:.1f}%",
                    "meta": {"exit_kind": "peak_drawdown", "peak_pct": prev_peak, "pnl_pct": pnl_pct},
                },
            )
            # 매도 후 리셋
            svars[peak_key] = 0.0
            svars[activated_key] = False
        except (KeyError, AttributeError, TypeError, ValueError) as exc:
            logger.error("[SELL] post-sell peak drawdown state reset failed: %s", exc, exc_info=True)

    # [5D] _get_effective_tp_for_market → hs_mixin_guards.py

    async def _run_global_profit_poll(self) -> None:
        """[2026-02-04] Run Global Profit Take poll in background thread."""
        mgr = getattr(self, "ladder_manager", None)
        if mgr is None or not hasattr(mgr, "poll_global_profit_take"):
            return

        if self._global_profit_poll_lock.locked():
            return

        async with self._global_profit_poll_lock:
            try:
                await asyncio.to_thread(mgr.poll_global_profit_take)
            except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
                try:
                    self.ledger.append("GLOBAL_PROFIT_POLL_ERROR", error=str(exc))
                except (AttributeError, TypeError, ValueError):
                    logger.warning("[GlobalProfit] poll_global_profit_take 원장 기록도 실패: %s", exc)

    # --------------------------------------------------------
    # Reserved Autopilot (OMA maintenance)
    # --------------------------------------------------------
    def _autopilot_in_window(self, ts: Optional[float] = None) -> bool:
        '''Return True if current local time is within configured autopilot window.'''
        if not bool(getattr(self, "autopilot_window_enabled", False)):
            return True

        def _parse_hhmm(s: str) -> Optional[int]:
            try:
                parts = str(s).strip().split(":")
                if len(parts) != 2:
                    return None
                hh = int(parts[0])
                mm = int(parts[1])
                if hh < 0 or hh > 23 or mm < 0 or mm > 59:
                    return None
                return hh * 60 + mm
            except (TypeError, ValueError):
                logger.warning("[_is_in_autopilot_window] failed to parse HH:MM time string", exc_info=True)
                return None

        start_s = str(getattr(self, "autopilot_window_start", "22:00") or "22:00")
        end_s = str(getattr(self, "autopilot_window_end", "08:00") or "08:00")
        sm = _parse_hhmm(start_s)
        em = _parse_hhmm(end_s)
        if sm is None or em is None:
            return True

        lt = time.localtime(ts if ts is not None else time.time())
        cur = lt.tm_hour * 60 + lt.tm_min

        # Same-day window
        if sm <= em:
            return sm <= cur <= em

        # Overnight window (e.g., 22:00 ~ 08:00)
        return (cur >= sm) or (cur <= em)

    async def _autopilot_loop(self):
        '''Periodic autopilot runner.

        This task is always started, but it becomes a no-op unless:
        - autopilot_enabled == True
        - (optional) autopilot_window_enabled == True and time is within window
        
        [2026-02-01] AutopilotManager 사용: 모든 전략 지원 + SNIPER 급등 스캐너
        '''
        # AutopilotManager가 있으면 그쪽 루프 사용
        if hasattr(self, 'autopilot_manager') and self.autopilot_manager is not None:
            await self.autopilot_manager.start()
            # AutopilotManager._loop()가 자체 루프를 돌리므로 여기서는 먼지 청소만 체크
            while True:
                await asyncio.sleep(60.0)
                # [2026-02-01] 자동 먼지 청소 체크
                await self._check_auto_dust_vacuum()
        
        # 기존 로직 (fallback)
        # [2026-03-07] Scope 독립 타이머
        _scope_last_run_ts: float = 0.0
        _SCOPE_INDEPENDENT_INTERVAL: int = 60

        while True:
            try:
                await asyncio.sleep(1.0)

                now = time.time()
                autopilot_on = bool(getattr(self, "autopilot_enabled", False))

                # ── [2026-03-07] Scope 독립 루프: autopilot OFF여도 빈 슬롯 자동충원 ──
                scope_rotation_en = bool(getattr(self, "autopilot_scope_rotation_enabled", True))
                scope_target = max(0, int(
                    getattr(self, "autopilot_scope_target_n",
                            getattr(self, "reserved_sniper_n", 0)) or 0))
                if (scope_rotation_en
                        and scope_target > 0
                        and not autopilot_on
                        and not getattr(self, "_autopilot_inflight", False)
                        and (now - _scope_last_run_ts) >= _SCOPE_INDEPENDENT_INTERVAL):
                    _scope_last_run_ts = now
                    try:
                        from app.manager.autopilot_manager import AutopilotManager
                        scope_helper = getattr(self, "_scope_rotation_helper", None)
                        if scope_helper is None:
                            scope_helper = AutopilotManager(system=self)
                            self._scope_rotation_helper = scope_helper
                        scope_idle_min = max(2, int(
                            getattr(self, "autopilot_scope_idle_min", 2) or 2))
                        scope_result = await scope_helper._step_scope_slot_rotation(
                            now=now, idle_min=scope_idle_min)
                        if scope_result:
                            import logging
                            logging.getLogger(__name__).info(
                                f"[Autopilot/ScopeIndependent] scope rotation "
                                f"({len(scope_result)} actions) while autopilot OFF")
                    except (ImportError, AttributeError, TypeError) as exc:
                        logger.warning("[AUTOPILOT] independent scope rotation loop failed: %s", exc, exc_info=True)

                # ── 기존 Autopilot 메인 루프 ──
                if not autopilot_on:
                    continue

                # time window gate
                if not self._autopilot_in_window(now):
                    continue

                interval = int(getattr(self, "autopilot_eval_interval_sec", 300) or 300)
                if interval < 5:
                    interval = 5

                last = float(getattr(self, "autopilot_last_run_ts", 0.0) or 0.0)
                if last and (now - last) < interval:
                    continue

                if getattr(self, "_autopilot_inflight", False):
                    continue

                await self.autopilot_step(reason="loop", scan_only=False)

            except asyncio.CancelledError:
                raise
            except (ImportError, AttributeError, TypeError) as exc:
                try:
                    self.ledger.append("AUTOPILOT_LOOP_ERROR", error=str(exc))
                except (AttributeError, TypeError, ValueError) as exc:
                    logger.warning("[AUTOPILOT] ledger append for loop error failed: %s", exc)

    async def autopilot_step(self, *, reason: str = "loop", scan_only: bool = False) -> Dict[str, Any]:
        '''Run a single autopilot maintenance step.

        Responsibilities:
        - Scan Bybit and refresh Reserved Queue (best-effort)
        - Demote idle ACTIVE markets to WATCH/RECOVERY (optional)
        - Auto-approve Reserved candidates to fill per-strategy targets (optional)

        Safety:
        - This method does not place orders directly.
        - Promotion to ACTIVE still goes through Coordinator warm-up gating.
        '''
        if self._autopilot_lock.locked():
            return {"ok": False, "error": "inflight"}

        async with self._autopilot_lock:
            return await self._autopilot_step_inner(reason=reason, scan_only=scan_only)

    async def _autopilot_step_inner(self, *, reason: str = "loop", scan_only: bool = False):
        '''Inner implementation of autopilot_step, protected by _autopilot_lock.'''
        self._autopilot_inflight = True
        t0 = time.time()
        now = time.time()

        # prune expired cooldown entries (best-effort)
        try:
            self._autopilot_cooldown_prune(now_ts=now)
        except (AttributeError, TypeError, ValueError) as exc:
            logger.warning("[AUTOPILOT] cooldown prune of expired entries failed: %s", exc, exc_info=True)

        # Record "last run" early to avoid tight retry loops on errors
        try:
            self.autopilot_last_run_ts = now
        except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
            logger.warning("[AUTOPILOT] record last_run_ts failed: %s", exc, exc_info=True)

        result: Dict[str, Any] = {"ok": True, "reason": str(reason), "scan_only": bool(scan_only)}
        try:
            # gates
            enabled = bool(getattr(self, "autopilot_enabled", False))
            auto_approve = bool(getattr(self, "autopilot_auto_approve", False))
            # [FIX] auto_approve가 꺼져있으면 API 호출도 skip (scan_only는 허용)
            if (not scan_only) and (not auto_approve):
                result.update({"skipped": True, "skip_reason": "auto_approve_disabled"})
                return result
            if (not scan_only) and (not enabled) and (str(reason).lower() not in ("manual", "api", "debug")):
                result.update({"skipped": True, "skip_reason": "disabled"})
                return result

            # NOTE: time-window gating is enforced by _autopilot_loop().
            # Manual/API calls should not be blocked by window.
            #
            # (If you need strict window gating for manual runs as well, move the
            #  _autopilot_in_window() check back into this function.)

            # Emergency stop: do not auto-promote/auto-demote markets (operator only)
            if (not scan_only) and bool(getattr(self, "emergency_stop", False)):
                result.update({"skipped": True, "skip_reason": "emergency_stop"})
                return result

            # Settings
            pp_target = max(0, int(getattr(self, "reserved_pingpong_n", 0) or 0))
            al_target = max(0, int(getattr(self, "reserved_autoloop_n", 0) or 0))
            ld_target = max(0, int(getattr(self, "reserved_ladder_n", 0) or 0))
            lt_target = max(0, int(getattr(self, "reserved_lightning_n", 0) or 0))
            gz_target = max(0, int(getattr(self, "reserved_gazua_n", 0) or 0))
            ct_target = max(0, int(getattr(self, "reserved_contrarian_n", 0) or 0))
            sn_target = max(0, int(getattr(self, "reserved_sniper_n", 0) or 0))
            promote_to_active = bool(getattr(self, "reserved_promote_to_active", False))
            apply_budget = bool(getattr(self, "reserved_apply_suggested_budget", True))

            auto_approve = bool(getattr(self, "autopilot_auto_approve", False))
            idle_en = bool(getattr(self, "autopilot_idle_demote_enabled", False))
            idle_min = max(0, int(getattr(self, "autopilot_idle_demote_min", 0) or 0))
            grace_sec = max(0, int(getattr(self, "autopilot_grace_sec", 0) or 0))
            demote_max_total = max(0, int(getattr(self, "autopilot_demote_max_total", 0) or 0))
            demote_max_per_strategy = max(0, int(getattr(self, "autopilot_demote_max_per_strategy", 0) or 0))

            # Step 1) Scan Bybit and refresh Reserved Queue (best-effort, non-blocking)
            scan_summary: Dict[str, Any] = {}
            # [FIX 2026-03-22] 트리아지 활성 중에는 스캔 스킵 (BUY 차단 중이라 낭비)
            if getattr(self, "_triage_entry_blocked", False):
                scan_summary = {"skipped": True, "reason": "triage_mode_active"}
            else:
                try:
                    from app.manager.reserved_selector import build_reserved_candidates
                    from app.manager.reserved_queue import reserved_queue

                    scan_t0 = time.time()
                    async with self._scan_gate:
                        _loop = asyncio.get_event_loop()
                        items, summary = await _loop.run_in_executor(
                            self._scan_executor,
                            functools.partial(
                                build_reserved_candidates, self,
                                pingpong_n=pp_target, autoloop_n=al_target,
                                ladder_n=ld_target, lightning_n=lt_target,
                                gazua_n=gz_target, contrarian_n=ct_target, sniper_n=sn_target
                            )
                        )
                    summary = dict(summary or {})
                    summary["elapsed_sec"] = round(time.time() - scan_t0, 3)

                    reserved_queue.replace(items, summary=summary)
                    scan_summary = summary

                    # Visibility (AutoApprove can consume queue instantly)
                    try:
                        reserved_queue.add_history({
                            "kind": "SCAN",
                            "source": "autopilot",
                            "picked_pingpong": int(summary.get("picked_pingpong") or 0),
                            "picked_autoloop": int(summary.get("picked_autoloop") or 0),
                            "picked_ladder": int(summary.get("picked_ladder") or 0),
                            "picked_lightning": int(summary.get("picked_lightning") or 0),
                            "picked_gazua": int(summary.get("picked_gazua") or 0),
                            "picked_contrarian": int(summary.get("picked_contrarian") or 0),
                            "picked_sniper": int(summary.get("picked_sniper") or 0),
                            "elapsed_sec": summary.get("elapsed_sec"),
                        })
                    except (KeyError, AttributeError, TypeError, ValueError) as exc:
                        logger.warning("[AUTOPILOT] ledger append for auto-approve visibility failed: %s", exc)
                except (ImportError, AttributeError, TypeError) as exc:
                    logger.warning("[autopilot_step] scan failed: %s", exc, exc_info=True)
                    scan_summary = {"ok": False, "error": str(exc)}

            result["scan_summary"] = scan_summary

            if scan_only:
                return result

            # Step 2) Active market map + strategy inference
            snap = {}
            try:
                snap = self.oma_registry.snapshot()
            except (AttributeError, TypeError):
                logger.warning("[autopilot_step] failed to get oma_registry snapshot", exc_info=True)
                snap = {}

            active_rows = snap.get("active") or []
            active_reason_map: Dict[str, List[str]] = {}
            active_markets: List[str] = []

            for row in active_rows:
                if isinstance(row, dict):
                    m = str(row.get("market") or "").strip().upper()
                    if not m:
                        continue
                    active_markets.append(m)
                    rs = row.get("reason")
                    if isinstance(rs, list):
                        active_reason_map[m] = [str(x) for x in rs]
                    else:
                        active_reason_map[m] = []
                elif isinstance(row, str):
                    m = str(row).strip().upper()
                    if m:
                        active_markets.append(m)

            def _infer_strategy(market: str) -> str:
                STRATEGY_KEYWORDS = ["PINGPONG", "AUTOLOOP", "LADDER", "LIGHTNING", "GAZUA", "CONTRARIAN", "SNIPER"]
                
                # 1) reason tag - "strategy:XXX" 형식 우선
                rs = active_reason_map.get(market) or []
                for r in rs:
                    if isinstance(r, str) and r.upper().startswith("STRATEGY:"):
                        return r.split(":", 1)[1].strip().upper() or "UNKNOWN"

                # 1b) reason tag - 키워드 패턴 매칭 (예: "pingpong_budget_restore", "sniper_budget_restore")
                for r in rs:
                    if isinstance(r, str):
                        r_upper = r.upper()
                        for kw in STRATEGY_KEYWORDS:
                            if kw in r_upper:
                                return kw

                # 2) ctx.controls.strategy.mode (Reserved approvals set this)
                try:
                    ctx = self.coordinator.contexts.get(market)
                    ctrls = getattr(ctx, "controls", {}) or {}
                    sc = ctrls.get("strategy") or {}
                    if isinstance(sc, dict) and bool(sc.get("enabled")):
                        md = str(sc.get("mode") or "").strip().upper()
                        if md:
                            return md
                except (KeyError, AttributeError, TypeError, ValueError) as exc:
                    logger.warning("[AUTOPILOT] strategy mode lookup from context controls failed: %s", exc, exc_info=True)

                # 3) fallback
                try:
                    ctx = self.coordinator.contexts.get(market)
                    sel = str(getattr(ctx, "selected_strategy", "") or "").strip().upper()
                    if sel:
                        return sel
                except (KeyError, AttributeError, TypeError, ValueError) as exc:
                    logger.warning("[AUTOPILOT] strategy mode fallback lookup failed: %s", exc, exc_info=True)
                return "UNKNOWN"

            # Step 3) Demote idle markets (optional)
            demoted: List[Dict[str, Any]] = []
            if idle_en and idle_min > 0:
                # Determine max window to cover all strategies
                max_idle = idle_min
                overrides = getattr(self, "autopilot_idle_demote_overrides", {}) or {}
                if overrides:
                    max_idle = max(max_idle, max(overrides.values()))
                
                max_window_sec = int(max_idle) * 60
                since_ts = now - float(max_window_sec)

                # ledger tail read (I/O) in thread
                records: List[Dict[str, Any]] = []
                try:
                    records = await asyncio.to_thread(
                        functools.partial(self.ledger.tail_records, since_ts=since_ts, tail_lines=int(os.getenv("OMA_AUTOPILOT_IDLE_TAIL_LINES", "50000")))
                    )
                except (TypeError, ValueError):
                    logger.warning("[autopilot_step] idle demote: failed to read ledger tail records", exc_info=True)
                    records = []

                last_fill_ts_map: Dict[str, float] = {}
                for rec in records:
                    try:
                        ev = str(rec.get("event") or "")
                        if ev not in ("FILL_BUY", "FILL_SELL"):
                            continue
                        mk = str(rec.get("market") or rec.get("data", {}).get("market") or "").strip().upper()
                        if not mk:
                            continue
                        ts = float(rec.get("ts") or 0.0)
                        if ts > last_fill_ts_map.get(mk, 0.0):
                            last_fill_ts_map[mk] = ts
                    except (KeyError, TypeError, ValueError, AttributeError) as exc:
                        logger.warning("[AUTOPILOT] ledger tail read for last fill timestamp failed: %s", exc)
                        continue

                # Candidates = ACTIVE markets with 0 fills within window, past grace period
                candidates: List[Tuple[float, str, str]] = []  # (age_sec, strategy, market)
                for mkt in active_markets:
                    try:
                        since_active = float(self.oma_registry.get_active_since_ts(mkt) or 0.0)
                    except (TypeError, ValueError):
                        logger.warning("[autopilot_step] idle demote: failed to parse active_since_ts for %s", mkt, exc_info=True)
                        since_active = 0.0

                    strat = _infer_strategy(mkt)
                    
                    # [HARD EXCLUDE] Do NOT demote LADDER strategy coins to LongHold, regardless of position or idle time
                    # This ensures that actively trading grid/ladder coins are never moved to LongHold or demoted due to inactivity.
                    if strat == "LADDER":
                        continue

                    # Strategy-specific idle limit
                    limit_min = overrides.get(strat, idle_min)
                    limit_sec = limit_min * 60

                    # Check idle condition
                    last_fill = last_fill_ts_map.get(mkt, 0.0)
                    # Idle time is time since last fill OR time since activation (if no fills)
                    idle_duration = now - max(last_fill, since_active)
                    
                    age = (now - since_active) if since_active > 0 else 0.0 # Total active age
                    if grace_sec > 0 and age > 0 and age < grace_sec:
                        continue
                    
                    if idle_duration < limit_sec:
                        continue


                    candidates.append((age, strat, mkt))

                candidates.sort(key=lambda x: x[0], reverse=True)

                total_limit = demote_max_total if demote_max_total > 0 else 10_000
                per_limit = demote_max_per_strategy if demote_max_per_strategy > 0 else 10_000
                per_cnt: Dict[str, int] = {}

                for age, strat, mkt in candidates:
                    if len(demoted) >= total_limit:
                        break
                    if per_cnt.get(strat, 0) >= per_limit:
                        continue

                    try:
                        self.oma_set_market(
                            market=mkt,
                            state=MarketState.WATCH,
                            reason=[
                                "autopilot_demote_idle",
                                f"idle_min:{int(limit_min)}",
                                f"active_age_sec:{int(age)}",
                                f"source:{reason}",
                            ],
                        )
                        demoted.append({
                            "market": mkt,
                            "strategy": strat,
                            "active_age_sec": int(age),
                            "idle_min": int(limit_min),
                        })
                        per_cnt[strat] = int(per_cnt.get(strat, 0) + 1)

                        # cooldown mark (avoid immediate re-pick)
                        try:
                            self._autopilot_cooldown_mark(mkt, reason="demote_idle")
                        except (AttributeError, TypeError, ValueError) as exc:
                            logger.warning("[AUTOPILOT] cooldown mark after idle demote failed: %s", exc)

                        try:
                            from app.manager.reserved_queue import reserved_queue
                            reserved_queue.add_history({
                                "kind": "DEMOTE",
                                "source": "autopilot",
                                "market": mkt,
                                "strategy": strat,
                                "reason": "idle",
                                "idle_min": int(limit_min),
                                "active_age_sec": int(age),
                            })
                        except (TypeError, ValueError) as exc:
                            logger.warning("[AUTOPILOT] ledger append for idle demote failed: %s", exc)
                    except (KeyError, AttributeError, TypeError, ValueError) as exc:
                        try:
                            self.ledger.append("AUTOPILOT_DEMOTE_ERROR", market=mkt, error=str(exc))
                        except (AttributeError, TypeError, ValueError) as exc:
                            logger.warning("[AUTOPILOT] ledger append for demote error failed: %s", exc)

            # Step 3b) Demote churn/underperform markets (optional)
            # - 경험 기반: 거래는 많이 했는데(= 수수료/슬리피지만 먹는 패턴) 순이익이 거의 없으면 퇴출
            perf_en = bool(getattr(self, "autopilot_perf_demote_enabled", False))
            perf_window_min = max(0, int(getattr(self, "autopilot_perf_window_min", 0) or 0))
            if perf_en and perf_window_min > 0 and active_markets:
                perf_min_trades = max(0, int(getattr(self, "autopilot_perf_min_trades", 0) or 0))
                perf_min_sells = max(0, int(getattr(self, "autopilot_perf_min_sells", 0) or 0))
                try:
                    perf_min_net_cash = float(getattr(self, "autopilot_perf_min_net_cash_usdt", 0.0) or 0.0)
                except (AttributeError, TypeError, ValueError):
                    logger.warning("[autopilot_step] perf demote: failed to parse perf_min_net_cash", exc_info=True)
                    perf_min_net_cash = 0.0
                try:
                    perf_min_net_cash_per_trade = float(getattr(self, "autopilot_perf_min_net_cash_per_trade", 0.0) or 0.0)
                except (AttributeError, TypeError, ValueError):
                    logger.warning("[autopilot_step] perf demote: failed to parse perf_min_net_cash_per_trade", exc_info=True)
                    perf_min_net_cash_per_trade = 0.0

                window_sec = int(perf_window_min) * 60
                since_ts = now - float(window_sec)

                records: List[Dict[str, Any]] = []
                try:
                    records = await asyncio.to_thread(
                        functools.partial(self.ledger.tail_records, since_ts=since_ts, tail_lines=int(os.getenv("OMA_AUTOPILOT_PERF_TAIL_LINES", "80000")))
                    )
                except (TypeError, ValueError):
                    logger.warning("[autopilot_step] perf demote: failed to read ledger tail records", exc_info=True)
                    records = []

                try:
                    from app.manager.ledger_pnl import aggregate_fill_pnl
                except (ImportError, AttributeError, TypeError):
                    logger.warning("[autopilot_step] perf demote: failed to import aggregate_fill_pnl", exc_info=True)
                    aggregate_fill_pnl = None

                aggs: Dict[str, Dict[str, Any]] = {}
                if callable(aggregate_fill_pnl):
                    try:
                        aggs = aggregate_fill_pnl(records, since_ts=since_ts, until_ts=now, markets=active_markets)
                    except (AttributeError, TypeError, ValueError):
                        logger.warning("[autopilot_step] perf demote: aggregate_fill_pnl failed", exc_info=True)
                        aggs = {}

                # remaining demote budget (shared with idle demote)
                total_limit = demote_max_total if demote_max_total > 0 else 10_000
                per_limit = demote_max_per_strategy if demote_max_per_strategy > 0 else 10_000
                per_cnt2: Dict[str, int] = {}
                for d in demoted:
                    try:
                        s0 = str(d.get("strategy") or "UNKNOWN").upper()
                        per_cnt2[s0] = int(per_cnt2.get(s0, 0) + 1)
                    except (TypeError, ValueError, AttributeError) as exc:
                        logger.warning("[AUTOPILOT] per-strategy count during remaining demote budget calc failed: %s", exc)
                        continue

                perf_candidates: List[Tuple[float, float, int, str, str, float, int, int, int, float]] = []
                for mkt in active_markets:
                    # grace
                    try:
                        since_active = float(self.oma_registry.get_active_since_ts(mkt) or 0.0)
                    except (TypeError, ValueError):
                        logger.warning("[autopilot_step] perf demote: failed to parse active_since_ts for %s", mkt, exc_info=True)
                        since_active = 0.0
                    age = (now - since_active) if since_active > 0 else 0.0
                    if grace_sec > 0 and age > 0 and age < grace_sec:
                        continue

                    a = aggs.get(mkt) or {}
                    try:
                        trade_n = int(a.get("trade_n") or 0)
                        sell_n = int(a.get("sell_n") or 0)
                        buy_n = int(a.get("buy_n") or 0)
                    except (TypeError, ValueError):
                        logger.warning("[autopilot_step] perf demote: failed to parse trade counts for %s", mkt, exc_info=True)
                        trade_n = 0
                        sell_n = 0
                        buy_n = 0

                    if perf_min_trades > 0 and trade_n < perf_min_trades:
                        continue
                    if perf_min_sells > 0 and sell_n < perf_min_sells:
                        continue

                    try:
                        net_cash = float(a.get("net_cash_usdt") or 0.0)
                    except (TypeError, ValueError):
                        logger.warning("[autopilot_step] perf demote: failed to parse net_cash for %s", mkt, exc_info=True)
                        net_cash = 0.0
                    try:
                        fees = float(a.get("fees_usdt") or 0.0)
                    except (TypeError, ValueError):
                        logger.warning("[autopilot_step] perf demote: failed to parse fees for %s", mkt, exc_info=True)
                        fees = 0.0

                    net_per_trade = float(net_cash) / float(trade_n or 1)

                    # pass if it meets profitability thresholds
                    if float(net_cash) >= float(perf_min_net_cash):
                        continue
                    if float(perf_min_net_cash_per_trade) > 0 and float(net_per_trade) >= float(perf_min_net_cash_per_trade):
                        continue

                    strat = _infer_strategy(mkt)
                    # (sort key) worst first: net_per_trade asc, net_cash asc, trade_n desc
                    perf_candidates.append((float(net_per_trade), float(net_cash), int(trade_n), strat, mkt, float(fees), int(trade_n), int(buy_n), int(sell_n), float(age)))

                perf_candidates.sort(key=lambda x: (x[0], x[1], -x[2]))

                for net_per_trade, net_cash, trade_n0, strat, mkt, fees, trade_n, buy_n, sell_n, age in perf_candidates:
                    if len(demoted) >= total_limit:
                        break
                    if per_cnt2.get(strat, 0) >= per_limit:
                        continue

                    try:
                        self.oma_set_market(
                            market=mkt,
                            state=MarketState.WATCH,
                            reason=[
                                "autopilot_demote_underperf",
                                f"window_min:{int(perf_window_min)}",
                                f"net_cash_usdt:{round(float(net_cash), 2)}",
                                f"fees_usdt:{round(float(fees), 2)}",
                                f"trade_n:{int(trade_n)}",
                                f"buy_n:{int(buy_n)}",
                                f"sell_n:{int(sell_n)}",
                                f"active_age_sec:{int(age)}",
                                f"source:{reason}",
                            ],
                        )

                        demoted.append({
                            "market": mkt,
                            "strategy": strat,
                            "active_age_sec": int(age),
                            "rule": "underperf",
                            "window_min": int(perf_window_min),
                            "trade_n": int(trade_n),
                            "buy_n": int(buy_n),
                            "sell_n": int(sell_n),
                            "net_cash_usdt": float(net_cash),
                            "fees_usdt": float(fees),
                            "net_cash_per_trade_usdt": float(net_per_trade),
                        })
                        per_cnt2[strat] = int(per_cnt2.get(strat, 0) + 1)

                        # cooldown mark
                        try:
                            self._autopilot_cooldown_mark(mkt, reason="demote_underperf")
                        except (AttributeError, TypeError, ValueError) as exc:
                            logger.warning("[AUTOPILOT] cooldown mark after underperf demote failed: %s", exc)

                        try:
                            from app.manager.reserved_queue import reserved_queue
                            reserved_queue.add_history({
                                "kind": "DEMOTE",
                                "source": "autopilot",
                                "market": mkt,
                                "strategy": strat,
                                "reason": "underperf",
                                "window_min": int(perf_window_min),
                                "trade_n": int(trade_n),
                                "sell_n": int(sell_n),
                                "net_cash_usdt": float(net_cash),
                                "fees_usdt": float(fees),
                            })
                        except (AttributeError, TypeError, ValueError) as exc:
                            logger.warning("[AUTOPILOT] ledger append for underperf demote failed: %s", exc)

                    except (KeyError, AttributeError, TypeError, ValueError) as exc:
                        try:
                            self.ledger.append("AUTOPILOT_DEMOTE_ERROR", market=mkt, error=str(exc))
                        except (AttributeError, TypeError, ValueError) as exc:
                            logger.warning("[AUTOPILOT] ledger append for underperf demote error failed: %s", exc)

            result["demoted"] = demoted

            # Step 4) Auto-approve candidates to fill desired counts (optional)
            approved: List[Dict[str, Any]] = []
            any_target = pp_target > 0 or al_target > 0 or ld_target > 0 or lt_target > 0 or gz_target > 0 or ct_target > 0 or sn_target > 0
            # DEBUG: auto_approve 상태를 결과에 포함
            result["auto_approve_enabled"] = auto_approve
            result["any_target"] = any_target
            if auto_approve and any_target:
                try:
                    # refresh active after demote
                    snap2 = self.oma_registry.snapshot()
                    active_rows2 = snap2.get("active") or []
                    active_markets2: List[str] = []
                    active_reason_map2: Dict[str, List[str]] = {}
                    for row in active_rows2:
                        if isinstance(row, dict):
                            m = str(row.get("market") or "").strip().upper()
                            if not m:
                                continue
                            active_markets2.append(m)
                            rs = row.get("reason")
                            if isinstance(rs, list):
                                active_reason_map2[m] = [str(x) for x in rs]
                            else:
                                active_reason_map2[m] = []
                        elif isinstance(row, str):
                            m = str(row).strip().upper()
                            if m:
                                active_markets2.append(m)

                    active_reason_map.clear()
                    active_reason_map.update(active_reason_map2)
                    active_markets = active_markets2

                    active_pp = 0
                    active_al = 0
                    active_ld = 0
                    active_lt = 0
                    active_gz = 0
                    active_ct = 0
                    active_sn = 0
                    active_sn_scope = 0
                    for mkt in active_markets:
                        st = _infer_strategy(mkt)
                        if st == "PINGPONG": active_pp += 1
                        elif st == "AUTOLOOP": active_al += 1
                        elif st == "LADDER": active_ld += 1
                        elif st == "LIGHTNING": active_lt += 1
                        elif st == "GAZUA": active_gz += 1
                        elif st == "CONTRARIAN": active_ct += 1
                        elif st == "SNIPER": active_sn += 1
                        elif st == "SNIPER(S)": active_sn_scope += 1
                        else: active_gz += 1  # UNKNOWN은 GAZUA(수동 코인)로 간주
                    # SNIPER(s) scope 슬롯도 sn_target 예산을 공유 → 합산하여 초과 승인 방지
                    active_sn += active_sn_scope

                    need_pp = max(0, pp_target - active_pp)
                    need_al = max(0, al_target - active_al)
                    need_ld = max(0, ld_target - active_ld)
                    need_lt = max(0, lt_target - active_lt)
                    need_gz = max(0, gz_target - active_gz)
                    need_ct = max(0, ct_target - active_ct)
                    need_sn = max(0, sn_target - active_sn)

                    # [2026-03-08] SNIPER 시간 감쇠용 타이머: 빈 슬롯이 있으면 시작, 채워지면 리셋
                    if need_sn > 0:
                        if float(getattr(self, "_sniper_need_since_ts", 0.0) or 0.0) <= 0:
                            self._sniper_need_since_ts = now
                    else:
                        if float(getattr(self, "_sniper_need_since_ts", 0.0) or 0.0) > 0:
                            self._sniper_need_since_ts = 0.0

                    result["targets"] = {"PINGPONG": pp_target, "AUTOLOOP": al_target, "LADDER": ld_target, "LIGHTNING": lt_target, "GAZUA": gz_target, "CONTRARIAN": ct_target, "SNIPER": sn_target}
                    result["active_counts"] = {"PINGPONG": active_pp, "AUTOLOOP": active_al, "LADDER": active_ld, "LIGHTNING": active_lt, "GAZUA": active_gz, "CONTRARIAN": active_ct, "SNIPER": active_sn, "SNIPER_SCOPE": active_sn_scope}
                    result["needs"] = {"PINGPONG": need_pp, "AUTOLOOP": need_al, "LADDER": need_ld, "LIGHTNING": need_lt, "GAZUA": need_gz, "CONTRARIAN": need_ct, "SNIPER": need_sn}

                    any_need = need_pp > 0 or need_al > 0 or need_ld > 0 or need_lt > 0 or need_gz > 0 or need_ct > 0 or need_sn > 0
                    if any_need:
                        from app.manager.reserved_queue import reserved_queue
                        from app.manager.market_controls import apply_engine_controls

                        q = reserved_queue.snapshot()
                        items = list(q.get("items") or [])

                        # stable ordering: score desc (fallback: rank)
                        def _rank(it: Dict[str, Any]) -> float:
                            try:
                                return float(it.get("score") or it.get("rank") or 0.0)
                            except (TypeError, ValueError):
                                logger.warning("[autopilot_step] promote: failed to parse rank/score", exc_info=True)
                                return 0.0

                        items.sort(key=_rank, reverse=True)

                        to_state = MarketState.ACTIVE if promote_to_active else MarketState.WATCH

                        # 전략별 auto_approve 설정 확인
                        aa_pp = bool(getattr(self, "autopilot_auto_approve_pingpong", True))
                        aa_al = bool(getattr(self, "autopilot_auto_approve_autoloop", True))
                        aa_ld = bool(getattr(self, "autopilot_auto_approve_ladder", False))
                        aa_lt = bool(getattr(self, "autopilot_auto_approve_lightning", False))
                        aa_gz = bool(getattr(self, "autopilot_auto_approve_gazua", False))
                        aa_ct = bool(getattr(self, "autopilot_auto_approve_contrarian", False))
                        aa_sn = bool(getattr(self, "autopilot_auto_approve_sniper", False))
                        
                        # DEBUG: 전략별 auto_approve 상태 추가
                        result["per_strategy_auto_approve"] = {
                            "PINGPONG": aa_pp, "AUTOLOOP": aa_al, "LADDER": aa_ld,
                            "LIGHTNING": aa_lt, "GAZUA": aa_gz, "CONTRARIAN": aa_ct, "SNIPER": aa_sn
                        }
                        result["queue_items_count"] = len(items)

                        def _take(strategy: str, n: int, allowed: bool) -> List[Dict[str, Any]]:
                            if n <= 0 or not allowed:
                                return []
                            out: List[Dict[str, Any]] = []
                            for it in items:
                                if n <= 0:
                                    break
                                if str(it.get("strategy") or "").strip().upper() != strategy:
                                    continue
                                out.append(it)
                                n -= 1
                            return out

                        picks = (
                            _take("PINGPONG", need_pp, aa_pp) +
                            _take("AUTOLOOP", need_al, aa_al) +
                            _take("LADDER", need_ld, aa_ld) +
                            _take("LIGHTNING", need_lt, aa_lt) +
                            _take("GAZUA", need_gz, aa_gz) +
                            _take("CONTRARIAN", need_ct, aa_ct) +
                            _take("SNIPER", need_sn, aa_sn)
                        )

                        for it in picks:
                            rid = str(it.get("id") or it.get("rid") or "").strip()
                            if not rid:
                                continue

                            real = reserved_queue.pop(rid)
                            if not isinstance(real, dict):
                                continue

                            market = str(real.get("market") or "").strip().upper()
                            strategy = str(real.get("strategy") or "").strip().upper() or "UNKNOWN"

                            # [PATCH] Skip if already managed (Prevent overwriting manual setup)
                            try:
                                st = self.oma_registry.get_state(market)
                            except (AttributeError, TypeError):
                                logger.warning("[autopilot_step] promote: failed to get state for %s", market, exc_info=True)
                                st = None
                            if st in (MarketState.ACTIVE, MarketState.RECOVERY):
                                try:
                                    reserved_queue.push(real)
                                except (AttributeError, TypeError) as exc:
                                    logger.warning("[AUTOPILOT] reserved queue push for already-managed market failed: %s", exc)
                                continue

                            # Also skip if strategy is already enabled in context
                            try:
                                ctx = self.coordinator.contexts.get(market)
                            except (KeyError, AttributeError, TypeError):
                                logger.warning("[autopilot_step] promote: failed to get context for %s", market, exc_info=True)
                                ctx = None
                            if ctx is not None:
                                existing_strategy = ""
                                try:
                                    ctrls = getattr(ctx, "controls", {}) or {}
                                    strat_ctrl = ctrls.get("strategy", {}) or {}
                                    if bool(strat_ctrl.get("enabled")):
                                        existing_strategy = str(strat_ctrl.get("mode") or strat_ctrl.get("name") or "").strip().upper()
                                except (KeyError, AttributeError, TypeError, ValueError) as exc:
                                    logger.warning("[AUTOPILOT] existing strategy enabled check from context failed: %s", exc)
                                try:
                                    s_mode = str(getattr(ctx, "strategy_mode", "") or "").strip().upper()
                                    if s_mode and not existing_strategy:
                                        existing_strategy = s_mode
                                except (KeyError, AttributeError, TypeError) as exc:
                                    logger.warning("[AUTOPILOT] strategy mode fallback from context failed: %s", exc)
                                # 같은 전략이면 재승격 허용, 다른 전략이면 충돌로 스킵(일부일처제)
                                if existing_strategy and existing_strategy != strategy:
                                    try:
                                        reserved_queue.push(real)
                                    except (AttributeError, TypeError) as exc:
                                        logger.warning("[AUTOPILOT] reserved queue push for strategy conflict skip failed: %s", exc)
                                    continue

                            budget_usdt = None
                            if apply_budget:
                                try:
                                    b = float(real.get("suggested_budget_usdt") or 0.0)
                                    if b > 0:
                                        budget_usdt = b
                                except (KeyError, AttributeError, TypeError, ValueError):
                                    logger.warning("[autopilot_step] promote: failed to parse suggested_budget for %s", market, exc_info=True)
                                    budget_usdt = None

                            try:
                                # [2026-03-08] SNIPER 즉시매수: SNIPER(S)와 동일 구조
                                # 승인 시 confidence가 instant_buy_min_conf 이상이면 바로 매수
                                _sniper_instant = False
                                _sniper_conf = float(real.get("confidence") or 0.0)
                                _sniper_price = 0.0
                                _sniper_vol24 = 0.0
                                try:
                                    _m_metrics = real.get("metrics") or {}
                                    _sniper_price = float(_m_metrics.get("price") or 0.0)
                                    _sniper_vol24 = float(_m_metrics.get("vol24_usdt") or 0.0)
                                except (TypeError, ValueError, AttributeError) as exc:
                                    logger.warning("[AUTOPILOT] sniper instant buy metric extraction failed: %s", exc)

                                if strategy == "SNIPER" and _sniper_conf > 0:
                                    # BTC regime auto-adjustment (동일 로직)
                                    _inst_base_raw = getattr(self, "autopilot_scope_instant_buy_min_conf", None)
                                    try:
                                        _inst_base = max(30.0, min(95.0, float(_inst_base_raw or 55.0)))
                                    except (TypeError, ValueError):
                                        logger.warning("[autopilot_step] promote: failed to parse instant_buy_min_conf", exc_info=True)
                                        _inst_base = 55.0
                                    _regime = "TREND"
                                    try:
                                        _regime = str(getattr(self, "_btc_regime", "TREND") or "TREND").upper()
                                    except (AttributeError, TypeError) as exc:
                                        logger.warning("[AUTOPILOT] BTC regime attribute read for auto-adjustment failed: %s", exc)
                                    _regime_adj_map = {"TREND": 0.0, "RECOVERY": -3.0, "DRIFT": -5.0, "SHOCK": 10.0}
                                    _inst_after_regime = max(30.0, min(95.0, _inst_base + _regime_adj_map.get(_regime, 0.0)))

                                    # [2026-03-08] 시간 감쇠: need_sn > 0 대기 시간에 따라 기준 완화
                                    # 10분마다 -2%p, 최저 = 설정값 * 0.7
                                    _sn_need_since = float(getattr(self, "_sniper_need_since_ts", 0.0) or 0.0)
                                    if _sn_need_since > 0:
                                        _sn_wait_min = (now - _sn_need_since) / 60.0
                                        _sn_decay = (_sn_wait_min / 10.0) * 2.0
                                        _sn_floor = _inst_base * 0.7
                                        _inst_min_conf = max(_sn_floor, _inst_after_regime - _sn_decay)
                                        _inst_min_conf = max(30.0, min(95.0, _inst_min_conf))
                                    else:
                                        _inst_min_conf = _inst_after_regime

                                    if _sniper_conf >= _inst_min_conf:
                                        _sniper_instant = True
                                        # 동적 예산 계산 (Root Cause 4 동일 로직)
                                        _dyn_base = float(budget_usdt or 100.0)
                                        if _sniper_conf >= 85: _c_mul = 1.5
                                        elif _sniper_conf >= 75: _c_mul = 1.2
                                        elif _sniper_conf >= 65: _c_mul = 1.0
                                        elif _sniper_conf >= 55: _c_mul = 0.8
                                        else: _c_mul = 0.6
                                        _p_mul = 1.0
                                        if _sniper_price < 100: _p_mul = 0.5
                                        elif _sniper_price < 500: _p_mul = 0.7
                                        elif _sniper_price < 1000: _p_mul = 0.85
                                        _l_mul = 1.0
                                        if _sniper_vol24 < 500_000: _l_mul = 0.5
                                        elif _sniper_vol24 < 1_000_000: _l_mul = 0.7
                                        elif _sniper_vol24 < 3_000_000: _l_mul = 0.85
                                        budget_usdt = max(Q.min_order, min(_dyn_base * 2.0, round(_dyn_base * _c_mul * _p_mul * _l_mul, 0)))

                                self.oma_set_market(
                                    market=market,
                                    state=to_state,
                                    reason=[
                                        "reserved_approve",
                                        "autopilot_autoapprove",
                                        f"strategy:{strategy}",
                                        f"source:{reason}",
                                    ],
                                    budget_usdt=budget_usdt,
                                )

                                try:
                                    apply_engine_controls(self, market, strategy,
                                                          recommended_params=real.get("recommended_params"))
                                except (KeyError, AttributeError, TypeError) as exc:
                                    logger.warning("[AUTOPILOT] apply_engine_controls for approved market failed: %s", exc, exc_info=True)

                                # [2026-03-08] SNIPER 즉시매수 실행
                                # [FIX 2026-03-22] 트리아지 모드 중에는 즉시매수 차단
                                # SNIPER는 _handle_intent() 면제 전략이지만, instant buy는
                                # _handle_intent()를 완전 우회하므로 별도 체크 필요.
                                # 트리아지 DCA 예산 보전을 위해 차단.
                                _instant_buy_ok = False
                                if _sniper_instant and getattr(self, "_triage_entry_blocked", False):
                                    _sniper_instant = False
                                    logger.debug("[autopilot] SNIPER instant buy blocked: triage mode active, market=%s", market)
                                if _sniper_instant:
                                    try:
                                        from app.core.hyper_price_store import price_store
                                        _cur_price = float(price_store.get_price(market) or 0)
                                        _fsm = getattr(self, "order_fsm", None)
                                        if _fsm and _cur_price > 0 and budget_usdt and budget_usdt >= 5:
                                            _ok, _msg = _fsm.submit_market_buy(
                                                ctx=self.coordinator.contexts.get(market),
                                                market=market,
                                                usdt_amount=budget_usdt,
                                                expected_price=_cur_price,
                                                reason="sniper:reserved_instant_buy",
                                            )
                                            _instant_buy_ok = bool(_ok)
                                            if _ok:
                                                self.ledger.append("SNIPER_INSTANT_BUY", market=market,
                                                    confidence=_sniper_conf, budget=budget_usdt,
                                                    price=_cur_price, regime=_regime)
                                                # grace period: sniper_active_ts 즉시 설정
                                                try:
                                                    _ctx_ib = self.coordinator.contexts.get(market)
                                                    if _ctx_ib and hasattr(_ctx_ib, "set_var"):
                                                        _ctx_ib.set_var("sniper_active_ts", now)
                                                except (KeyError, AttributeError, TypeError) as exc:
                                                    logger.warning("[SNIPER] sniper_active_ts context variable set failed: %s", exc)
                                    except (KeyError, AttributeError, TypeError, ValueError) as _buy_exc:
                                        try:
                                            self.ledger.append("SNIPER_INSTANT_BUY_ERROR", market=market,
                                                error=str(_buy_exc))
                                        except (AttributeError, TypeError, ValueError) as exc:
                                            logger.warning("[SNIPER] ledger append for instant buy error failed: %s", exc)

                                approved.append({
                                    "rid": rid,
                                    "market": market,
                                    "strategy": strategy,
                                    "to_state": str(to_state.value),
                                    "budget_usdt": budget_usdt,
                                    "instant_buy": _sniper_instant,
                                    "instant_buy_ok": _instant_buy_ok,
                                    "confidence": _sniper_conf,
                                })

                                try:
                                    reserved_queue.add_history({
                                        "kind": "APPROVE",
                                        "source": "autopilot",
                                        "rid": rid,
                                        "market": market,
                                        "strategy": strategy,
                                        "to_state": str(to_state.value),
                                        "auto": True,
                                        "instant_buy": _sniper_instant,
                                    })
                                except (AttributeError, TypeError, ValueError) as exc:
                                    logger.warning("[AUTOPILOT] ledger append for approval record failed: %s", exc)

                            except Exception as exc:
                                # push back on failure
                                try:
                                    reserved_queue.push(real)
                                except (AttributeError, TypeError) as exc:
                                    logger.warning("[AUTOPILOT] reserved queue push-back on approval failure: %s", exc)
                                try:
                                    reserved_queue.add_history({
                                        "kind": "APPROVE_FAIL",
                                        "source": "autopilot",
                                        "rid": rid,
                                        "market": market,
                                        "strategy": strategy,
                                        "error": str(exc),
                                    })
                                except (AttributeError, TypeError, ValueError) as exc:
                                    logger.warning("[AUTOPILOT] ledger append for approval telemetry on failure: %s", exc)
                                try:
                                    self.ledger.append("AUTOPILOT_APPROVE_ERROR", market=market, error=str(exc))
                                except (AttributeError, TypeError, ValueError) as exc:
                                    logger.warning("[AUTOPILOT] ledger append for approval error record failed: %s", exc)
                except Exception as exc:
                    try:
                        self.ledger.append("AUTOPILOT_STEP_ERROR", error=str(exc))
                    except (AttributeError, TypeError, ValueError) as exc:
                        logger.warning("[AUTOPILOT] ledger append for step-level error failed: %s", exc)

            # Step 4b) Scope Slot Rotation / Autofill for SNIPER(s)
            scope_rotated: List[Dict[str, Any]] = []
            scope_rotation_en = bool(getattr(self, "autopilot_scope_rotation_enabled", True))
            scope_idle_min = max(2, int(getattr(self, "autopilot_scope_idle_min", 2) or 2))
            if scope_rotation_en:
                try:
                    # HyperSystem은 현재 autopilot_step 경로를 사용하므로
                    # SNIPER(s) 전용 자동충원 로직만 helper로 재사용한다.
                    from app.manager.autopilot_manager import AutopilotManager
                    scope_helper = getattr(self, "_scope_rotation_helper", None)
                    if scope_helper is None:
                        scope_helper = AutopilotManager(system=self)
                        self._scope_rotation_helper = scope_helper
                    scope_rotated = await scope_helper._step_scope_slot_rotation(
                        now=now,
                        idle_min=scope_idle_min,
                    )
                except (ImportError, AttributeError, TypeError) as exc:
                    try:
                        self.ledger.append("AUTOPILOT_SCOPE_ROTATION_ERROR", error=str(exc))
                    except (AttributeError, TypeError, ValueError) as exc:
                        logger.warning("[AUTOPILOT] ledger append for scope rotation error failed: %s", exc)

            result["scope_rotated"] = scope_rotated
            result["approved"] = approved
            return result

        finally:
            try:
                result["elapsed_sec"] = round(time.time() - t0, 3)
            except (TypeError, ValueError, OverflowError) as exc:
                logger.warning("[AUTOPILOT] elapsed_sec calculation for result failed: %s", exc)

            try:
                self.autopilot_last_result = dict(result)
            except (TypeError, ValueError):
                logger.warning("[autopilot_step] failed to convert result to dict", exc_info=True)
                self.autopilot_last_result = result

            self._autopilot_inflight = False

    # [5F] background loop methods → hs_mixin_bg_loops.py

    async def _tick_loop(self):
        while True:
            t0 = time.perf_counter()
            try:
                now = time.time()
                # periodic reconcile — 비동기 오프로드 (REST API 블로킹 방지)
                if self.trade_client and (now - self._last_reconcile_ts) >= self.reconcile_interval_sec and not getattr(self, '_reconcile_inflight', False):
                    self._last_reconcile_ts = now
                    self._reconcile_inflight = True
                    async def _reconcile_wrapper():
                        try:
                            result = await asyncio.wait_for(
                                asyncio.get_running_loop().run_in_executor(
                                    self._bg_executor, lambda: self.reconcile(reason="periodic")),
                                timeout=60.0)
                            self._last_reconcile_result = result
                        except asyncio.TimeoutError:
                            logger.warning("[Reconcile] timeout 60s exceeded")
                        except Exception as exc:
                            self.ledger.append("RECONCILE_ERROR", error=str(exc), phase="periodic")
                        finally:
                            self._reconcile_inflight = False
                    asyncio.create_task(_reconcile_wrapper())

                # [2026-02-06] BTC Guard Mode - 5초마다 체크, 비동기 오프로드
                if self.btc_guard_enabled and (now - getattr(self, '_last_btc_guard_ts', 0.0)) >= 5.0 and not getattr(self, '_btc_guard_inflight', False):
                    self._last_btc_guard_ts = now
                    self._btc_guard_inflight = True
                    async def _do_btc_guard():
                        try:
                            await self._check_btc_guard_mode()
                        except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
                            self.ledger.append("BTC_GUARD_ERROR", error=str(exc))
                        finally:
                            self._btc_guard_inflight = False
                    asyncio.create_task(_do_btc_guard())

                # [2026-03-18] Recovery Boost 만료 체크
                if self.recovery_boost_active:
                    self._check_recovery_boost_expiry()
                
                # [Phase 3] Sniper Fast Lane — 30초마다 체크, 비동기 오프로드
                if getattr(self, '_sniper_fast_lane', None) is not None:
                    _fl = self._sniper_fast_lane
                    _fl_now = time.time()
                    if (_fl_now - getattr(self, '_last_fast_lane_ts', 0.0)) >= 30.0 and not getattr(self, '_fast_lane_inflight', False):
                        self._last_fast_lane_ts = _fl_now
                        self._fast_lane_inflight = True
                        async def _do_fast_lane(_lane=_fl):
                            try:
                                activated = await asyncio.to_thread(_lane.check, self)
                                if activated:
                                    logger.info("[FastLane] activated: %s", activated)
                            except (AttributeError, TypeError, ValueError) as exc:
                                logger.warning("[FastLane] check error: %s", exc)
                            finally:
                                self._fast_lane_inflight = False
                        asyncio.create_task(_do_fast_lane())

                # LongHold (LADDER/GAZUA advisory) — periodic Telegram alerts
                try:
                    if bool(getattr(self, "longhold_alerts_enabled", False)):
                        interval = float(getattr(self, "longhold_poll_interval_sec", 30.0) or 30.0)
                        if interval <= 0:
                            interval = 30.0
                        now_ts = time.time()
                        last_ts = float(getattr(self, "_last_longhold_poll_ts", 0.0) or 0.0)
                        if (now_ts - last_ts) >= interval:
                            self._last_longhold_poll_ts = now_ts
                            asyncio.create_task(self._run_longhold_poll())
                except (OSError, KeyError, AttributeError, TypeError, ValueError, OverflowError) as exc:
                    try:
                        self.ledger.append("LONGHOLD_SCHEDULE_ERROR", error=str(exc))
                    except (AttributeError, TypeError, ValueError) as exc:
                        logger.warning("[LONGHOLD] ledger append for schedule error failed: %s", exc)

                # [2026-02-04] Global Profit Take poll — throttled (매 틱 task 생성 방지)
                try:
                    if bool(getattr(self, "global_profit_take", False)):
                        _gpt_now = time.time()
                        if (_gpt_now - getattr(self, "_global_profit_task_ts", 0.0)) >= 5.0 and not self._global_profit_poll_lock.locked():
                            self._global_profit_task_ts = _gpt_now
                            asyncio.create_task(self._run_global_profit_poll())
                except (OSError, KeyError, AttributeError, TypeError, ValueError, OverflowError) as exc:
                    logger.warning("[PROFIT] global profit take poll failed: %s", exc, exc_info=True)

                # [GridV2] LADDER auto sync is done below, after _rebalance_allocations

# markets to process: ACTIVE + RECOVERY
                active = self.oma_registry.list_active()
                # print(f"[ACTIVE-MARKETS] {active}")
                try:
                    recovery = self.oma_registry.list_recovery() if hasattr(self.oma_registry, "list_recovery") else []
                except (KeyError, AttributeError, TypeError):
                    logger.warning("[_tick_loop] failed to list recovery markets", exc_info=True)
                    recovery = []
                # [2026-05-30] 전략 배정된 WATCH 후보도 tick — 진입 조건 평가 위해.
                # 부모님 모델: WATCH=대상 후보 / 진입 조건 충족 시 ACTIVE.
                # 8553bef(승인=WATCH always) 이후 옛 ACTIVE 경로가 사라져 plugin 이 tick 누락됨.
                # ★ 안전: 스캔 universe 전체(441 "Bybit USDT Market" WATCH) 가 아니라
                #   engine controls.strategy.enabled=True 인 reserved 후보만 (슬롯 수로 bounded).
                # [2026-05-30 회귀 fix] reserved_watch(0f72327)는 autopilot(plugin 실구동) ON 일 때만.
                #   증상: 0f72327이 WATCH 후보를 tick 에 추가 → 매 tick 후보 가격을 가져옴.
                #   WS 피드 죽은 서버(사무실)는 후보당 REST 폴백 ~230ms × 18 = tick 4초+ (gather=total).
                #   WS 살아있는 서버(집)는 캐시라 무해(~ms). autopilot OFF면 plugin 진입도 없어
                #   후보 tick = 순수 낭비. (어제 백업 revert 로 코드가 원인임 확정 — 오늘 tick 경로 유일 변경.)
                reserved_watch = []
                if bool(getattr(self, "autopilot_enabled", False)):
                    try:
                        for _wm in self.oma_registry.list_watch():
                            _wc = self.coordinator.contexts.get(_wm)
                            _ws = ((getattr(_wc, "controls", None) or {}).get("strategy") or {}) if _wc else {}
                            if isinstance(_ws, dict) and _ws.get("enabled"):
                                reserved_watch.append(_wm)
                    except (KeyError, AttributeError, TypeError):
                        logger.warning("[_tick_loop] reserved WATCH enumerate failed", exc_info=True)
                        reserved_watch = []
                markets = list(dict.fromkeys(active + recovery + reserved_watch))

                if not markets:
                    self._last_tick_duration = time.perf_counter() - t0
                    self._tick_count += 1
                    await asyncio.sleep(max(0.0, self.tick_interval_sec - self._last_tick_duration))
                    continue

                # soft capital allocation (ACTIVE only) — throttled, 비동기 오프로드
                _now_rb = time.time()
                if (_now_rb - self._last_rebalance_ts) >= self._rebalance_interval_sec and not getattr(self, '_rebalance_inflight', False):
                    self._last_rebalance_ts = _now_rb
                    self._rebalance_inflight = True
                    _rb_markets = list(active)
                    async def _rebalance_wrapper(_m=_rb_markets):
                        try:
                            await asyncio.wait_for(
                                asyncio.get_running_loop().run_in_executor(
                                    self._bg_executor, lambda: self._rebalance_allocations(active_markets=_m)),
                                timeout=60.0)
                        except asyncio.TimeoutError:
                            logger.warning("[Rebalance] timeout 60s exceeded")
                        except Exception as exc:
                            logger.warning("[REBALANCE] rebalance execution failed: %s", exc, exc_info=True)
                        finally:
                            self._rebalance_inflight = False
                    asyncio.create_task(_rebalance_wrapper())

                # [GridV2] LADDER: active window 기반 sync — throttled, 백그라운드 오프로드 (tick 블로킹 방지)
                if (_now_rb - self._last_ladder_sync_ts) >= self._ladder_sync_interval_sec and not getattr(self, '_ladder_sync_inflight', False):
                    self._last_ladder_sync_ts = _now_rb
                    self._ladder_sync_inflight = True
                    async def _ladder_sync_wrapper():
                        try:
                            await asyncio.wait_for(
                                asyncio.get_running_loop().run_in_executor(
                                    self._bg_executor, self._tick_ladder_grid_sync),
                                timeout=60.0)
                        except asyncio.TimeoutError:
                            logger.warning("[LadderSync] timeout 60s exceeded")
                        except Exception as exc:
                            logger.warning("GridV2 auto sync error: %s", exc)
                        finally:
                            self._ladder_sync_inflight = False
                    asyncio.create_task(_ladder_sync_wrapper())

                # Parallel execution
                _t_gather_start = time.perf_counter()
                # [PERF-TELEMETRY] 인디케이터 호출 카운터 리셋 + 캐시 클리어 (2026-03-21)
                try:
                    from app.strategy.indicators import reset_call_counts
                    from app.strategy.indicator_cache import clear as clear_indicator_cache
                    reset_call_counts()
                    clear_indicator_cache()
                except (ImportError, AttributeError, TypeError) as exc:
                    logger.warning("[PERF] indicator call counter reset and cache clear failed: %s", exc, exc_info=True)
                tasks = [self._process_market(m) for m in markets]
                if tasks:
                    # ★ Phase H (2026-04-20 형 letter#3 A-5): gather timeout — 1 market hang 시 전체 tick 차단 방지
                    try:
                        await asyncio.wait_for(
                            asyncio.gather(*tasks, return_exceptions=True),
                            timeout=30.0,
                        )
                    except asyncio.TimeoutError:
                        logger.error("[HYPER] tick gather timeout (>30s) — %d markets, possible market hang", len(tasks))
                _t_gather_ms = (time.perf_counter() - _t_gather_start) * 1000

                # [DIAG] 마켓 처리 시간 측정 — 매 tick 기록 (원인 추적용, 임시)
                _diag_full = getattr(self, '_diag_full_ticks', 0)
                _diag_skip = getattr(self, '_diag_skip_ticks', 0)
                _t_total_ms = (time.perf_counter() - t0) * 1000

                # [PERF-TELEMETRY] 확대 로깅 (2026-03-21): 모든 틱 기록 + 인디케이터 카운트 + coordinator 분해
                _ind_counts = {}
                try:
                    from app.strategy.indicators import get_call_counts
                    _ind_counts = get_call_counts()
                except (ImportError, AttributeError, TypeError) as exc:
                    logger.warning("[PERF] indicator call counts retrieval for telemetry failed: %s", exc)

                if _t_total_ms > 100:  # 기준점 측정을 위해 임계값 100ms로 낮춤 (기존 300ms)
                    # [2026-03-24] TICK_DIAG — 원장 기록 제거 (tick_perf.jsonl 전용 로그로 이관)
                    # 원장은 매매 이벤트 전용, 진단은 perf_ledger로 분리
                    pass
                self._diag_full_ticks = 0
                self._diag_skip_ticks = 0

                # [PERF-LOG] 전용 틱 성능 로그 — 매 틱 기록 (임계값 없음)
                if self._perf_ledger is not None:
                    _cache_stats = {}
                    try:
                        from app.strategy.indicator_cache import get_stats as _get_cache_stats
                        _cache_stats = _get_cache_stats()
                    except (ImportError, AttributeError, TypeError) as exc:
                        logger.warning("[PERF] cache stats retrieval for tick performance log failed: %s", exc)
                    self._perf_ledger.append("TICK",
                        total_ms=round(_t_total_ms, 1),
                        gather_ms=round(_t_gather_ms, 1),
                        markets=len(markets),
                        full=_diag_full,
                        skip=_diag_skip,
                        ind=_ind_counts or None,
                        cache=_cache_stats or None,
                    )

                # state flush (throttled)
                now = time.time()
                if (now - self._last_context_save_ts) >= self.context_state_save_interval_sec:
                    asyncio.get_running_loop().run_in_executor(self._bg_executor, self._save_context_state)
                    self._last_context_save_ts = now
                
                # Update cached ledger PnL (every 60s) — 비동기 오프로드 (블로킹 I/O 방지)
                if (now - self._cached_ledger_pnl_ts) >= 60.0 and not getattr(self, '_ledger_pnl_inflight', False):
                    self._cached_ledger_pnl_ts = now
                    self._ledger_pnl_inflight = True
                    _until_ts = now + 3600
                    async def _ledger_pnl_wrapper(_until=_until_ts):
                        try:
                            def _do_ledger_pnl():
                                from app.manager.ledger_pnl import aggregate_fill_pnl, load_pnl_baseline_ts
                                baseline_ts = load_pnl_baseline_ts()
                                if baseline_ts <= 0:
                                    baseline_ts = self._boot_ts
                                records = self.ledger.tail(2000)
                                aggs = aggregate_fill_pnl(records, since_ts=baseline_ts, until_ts=_until)
                                self._cached_ledger_pnl = sum(a.net_cash_usdt for a in aggs.values())
                            await asyncio.wait_for(
                                asyncio.get_running_loop().run_in_executor(self._bg_executor, _do_ledger_pnl),
                                timeout=60.0)
                        except asyncio.TimeoutError:
                            logger.warning("[LedgerPnL] timeout 60s exceeded")
                        except Exception as exc:
                            logger.warning("[LEDGER] ledger PnL calculation failed: %s", exc, exc_info=True)
                        finally:
                            self._ledger_pnl_inflight = False
                    asyncio.create_task(_ledger_pnl_wrapper())

                # Portfolio Risk Manager Update — throttled (30초), 비동기 오프로드
                if (now - self._last_portfolio_risk_ts) >= self._portfolio_risk_interval_sec and not getattr(self, '_portfolio_risk_inflight', False):
                    try:
                        if self.portfolio_risk_manager and self.portfolio_risk_manager.enabled:
                            current_capital = self._last_equity_usdt or 0.0
                            realized_pnl = self._cached_ledger_pnl
                            unrealized_pnl = 0.0
                            for m in active:
                                ctx_m = self.coordinator.get_context(m)
                                if ctx_m and hasattr(ctx_m, "position") and ctx_m.position:
                                    qty = float(ctx_m.position.get("quantity", 0.0) or 0.0)
                                    avg_buy = float(ctx_m.position.get("avg_buy_price", 0.0) or 0.0)
                                    current_price = price_store.get_price(m) or avg_buy
                                    unrealized_pnl += (current_price - avg_buy) * qty
                            self._last_portfolio_risk_ts = now
                            self._portfolio_risk_inflight = True
                            _prm = self.portfolio_risk_manager
                            async def _prm_wrapper(_cap=current_capital, _rpnl=realized_pnl, _upnl=unrealized_pnl, _p=_prm):
                                try:
                                    def _do_prm():
                                        if not _p.daily_status:
                                            _p.init_daily_status(_cap)
                                        _p.update_portfolio_pnl(
                                            current_capital=_cap,
                                            realized_pnl=_rpnl,
                                            unrealized_pnl=_upnl
                                        )
                                    await asyncio.wait_for(
                                        asyncio.get_running_loop().run_in_executor(self._bg_executor, _do_prm),
                                        timeout=60.0)
                                except asyncio.TimeoutError:
                                    logger.warning("[PortfolioRisk] timeout 60s exceeded")
                                except Exception as exc:
                                    logger.warning("[PRM] portfolio risk monitor execution failed: %s", exc, exc_info=True)
                                finally:
                                    self._portfolio_risk_inflight = False
                            asyncio.create_task(_prm_wrapper())
                    except Exception as exc:
                        logger.warning("[PRM] portfolio risk monitor task creation failed: %s", exc, exc_info=True)

                # Smart Alert Manager Update — throttled (60초), 비동기 오프로드
                if (now - self._last_smart_alert_ts) >= self._smart_alert_interval_sec and not getattr(self, '_smart_alert_inflight', False):
                    try:
                        if self.smart_alert_manager:
                            ledger_records = self.ledger.tail(2000)
                            self._last_smart_alert_ts = now
                            self._smart_alert_inflight = True
                            _sam = self.smart_alert_manager
                            async def _smart_alert_wrapper(_records=ledger_records, _s=_sam):
                                try:
                                    await asyncio.wait_for(
                                        asyncio.get_running_loop().run_in_executor(
                                            self._bg_executor, lambda: _s.check_and_send_daily_report(_records)),
                                        timeout=60.0)
                                except asyncio.TimeoutError:
                                    logger.warning("[SmartAlert] timeout 60s exceeded")
                                except Exception as exc:
                                    logger.warning("[ALERT] smart alert execution failed: %s", exc, exc_info=True)
                                finally:
                                    self._smart_alert_inflight = False
                            asyncio.create_task(_smart_alert_wrapper())
                    except Exception as exc:
                        logger.warning("[ALERT] smart alert task creation failed: %s", exc, exc_info=True)

                # Ladder auto-tune (periodic) — 비동기 오프로드
                if now - self._ladder_tune_last_ts >= self._ladder_tune_interval_sec and not getattr(self, '_ladder_tune_inflight', False):
                    self._ladder_tune_last_ts = now
                    lm = getattr(self, "ladder_manager", None)
                    if lm:
                        self._ladder_tune_inflight = True
                        async def _ladder_tune_wrapper(_lm=lm):
                            try:
                                def _do_ladder_tune():
                                    from app.manager.ladder_auto_tuner import LadderAutoTuner
                                    tuner = LadderAutoTuner(_lm, system=self)
                                    tuner.tune_all(dry_run=False)
                                await asyncio.wait_for(
                                    asyncio.get_running_loop().run_in_executor(self._bg_executor, _do_ladder_tune),
                                    timeout=60.0)
                            except asyncio.TimeoutError:
                                logger.warning("[LadderTune] timeout 60s exceeded")
                            except Exception as e:
                                logger.warning("ladder auto-tune failed: %s", e)
                            finally:
                                self._ladder_tune_inflight = False
                        asyncio.create_task(_ladder_tune_wrapper())

                # [TRIAGE MODE] 상태머신 폴 (5초 간격)
                _tm = getattr(self, "triage_manager", None)
                if _tm is not None:
                    _triage_interval = _tm.settings.get("check_interval_sec", 5.0)
                    if (now - getattr(self, "_last_triage_poll_ts", 0.0)) >= _triage_interval:
                        self._last_triage_poll_ts = now
                        if not getattr(self, "_triage_poll_inflight", False):
                            self._triage_poll_inflight = True
                            async def _triage_poll_wrapper(_tm=_tm):
                                try:
                                    await asyncio.wait_for(
                                        asyncio.get_running_loop().run_in_executor(
                                            self._bg_executor, lambda: _tm.poll(self)),
                                        timeout=60.0)
                                except asyncio.TimeoutError:
                                    logger.warning("[TriagePoll] timeout 60s exceeded")
                                except Exception as _te:
                                    logger.warning("[TRIAGE] triage poll execution failed: %s", _te, exc_info=True)
                                finally:
                                    self._triage_poll_inflight = False
                            asyncio.create_task(_triage_poll_wrapper())

            except Exception as e:
                self.ledger.append("TICK_LOOP_FATAL", error=str(e))

            # Measure duration
            duration = time.perf_counter() - t0
            self._last_tick_duration = duration
            self._tick_count += 1

            # [PATCH] Log slow ticks (> 2.0s) to detect overload — 쓰로틀 60초
            if duration > 2.0:
                _slow_elapsed = time.time() - getattr(self, '_last_slow_tick_log_ts', 0.0)
                if _slow_elapsed >= 60.0:
                    self.ledger.append("SLOW_TICK_DETECTED", duration_sec=round(duration, 3), active_markets=len(markets))
                    self._last_slow_tick_log_ts = time.time()

            # [PERF] Target Interval Sleep (2026-03-21)
            # 기존: 고정 지연 (tick_duration + interval = 낭비)
            # 변경: 목표 간격 수면 (interval - elapsed = 정확한 주기)
            _sleep_sec = max(0.0, self.tick_interval_sec - duration)
            await asyncio.sleep(_sleep_sec)

    # [5G] BTC guard / recovery boost methods → hs_mixin_btc_guard.py

    # [5E] budget/allocation methods → hs_mixin_budget.py
    # [5D] guard methods → hs_mixin_guards.py
    # (_infer_market_regime, _get_recent_high_price, _calc_entry_ceiling_price,
    #  _set_global_entry_cooldown, _check_drawdown_guard, _log_blocked_throttled,
    #  _send_telegram_safe, _notify_soft_once, _apply_ob_block_cooldown,
    #  _reset_ob_block_streak)

    # [5H] _handle_intent → hs_mixin_intent.py

    def run_tick(self, engine_name: str, market: str) -> Dict[str, Any]:
        """단일 시장에 대해 1회 tick을 실행하고 결과를 반환한다.

        목적:
        - 수동 테스트(REST) / 단위테스트 / 백테스트 유틸리티에서
          최소 정보(signal)뿐 아니라 position/policy 등 핵심 상태를
          함께 확인할 수 있도록 한다.

        주의:
        - TickLoop에서는 이 반환값을 사용하지 않는다.
        - 반환값 확장은 기존 호출자와의 호환을 깨지 않는다(키 추가).
        """
        price = price_store.get_price(market)
        if price is None:
            return {"ok": False, "error": f"no price for {market}"}

        # Context는 항상 준비 (status/테스트 용)
        ctx = self.coordinator.ensure_market(market)
        ctx.market_state = self.oma_registry.get_state(market).value
        ctx.trading_mode = self.trading_mode

        out = self.coordinator.tick(market, price)

        # ✅ 테스트/디버그 편의: 핵심 상태를 top-level로 노출
        if isinstance(out, dict):
            out.setdefault("position", ctx.position)
            out.setdefault("policy", ctx.policy)
            out.setdefault("readiness", ctx.readiness_status())
            out.setdefault("strategy", dict(getattr(ctx, "strategy_state", {}) or {}))
            out.setdefault("risk", dict(getattr(ctx, "risk_state", {}) or {}))
        else:
            out = {
                "signal": "hold",
                "engine_out": None,
                "position": ctx.position,
                "policy": ctx.policy,
                "readiness": ctx.readiness_status(),
                "strategy": dict(getattr(ctx, "strategy_state", {}) or {}),
                "risk": dict(getattr(ctx, "risk_state", {}) or {}),
            }

        return {
            "ok": True,
            "engine": self.ENGINE_NAME,
            "market": market,
            "price": price,
            "result": out,
        }


    # --------------------------------------------------------
    # Empirical tuning (ledger-based)
    # --------------------------------------------------------
    def tuning_report(self, *, window_hours: float = 24.0, min_samples: int = 50) -> Dict[str, Any]:
        """JSONL 원장 기반 튜닝 리포트.

        - slippage/latency/timeout/retry 분포를 산출한다.
        - 환경변수를 자동 변경하지 않고, 권장값만 산출한다.
        """
        try:
            from app.manager.ledger_tuner import TuningInput, build_tuning_report

            inp = TuningInput(
                ledger_path=self.ledger.path,
                window_hours=float(window_hours),
                min_samples=int(min_samples),
            )
            return build_tuning_report(inp)
        except (TypeError, ValueError) as exc:
            self.ledger.append("TUNING_REPORT_ERROR", error=str(exc))
            return {"ok": False, "error": str(exc)}

    def tuning_export_env(self, *, window_hours: float = 24.0, min_samples: int = 50) -> str:
        """tuning_report에서 산출한 ENV 권장 라인을 텍스트로 반환."""
        try:
            from app.manager.ledger_tuner import export_recommended_env

            report = self.tuning_report(window_hours=window_hours, min_samples=min_samples)
            return export_recommended_env(report)
        except (ImportError, AttributeError, TypeError) as exc:
            self.ledger.append("TUNING_EXPORT_ERROR", error=str(exc))
            return "# tuning export failed\n"

    def status(self) -> Dict[str, Any]:
        snap = self.oma_registry.snapshot()

        markets = [x.get("market") for x in snap.get("active", [])] + [x.get("market") for x in snap.get("recovery", [])]
        markets = [m for m in markets if m]

        prices = {m: price_store.get_price(m) for m in markets}

        # safety snapshot (best-effort; UI/monitoring용)
        base_eq: Optional[float] = None
        try:
            if self._principal_base_equity_usdt is not None and float(self._principal_base_equity_usdt) > 0:
                base_eq = float(self._principal_base_equity_usdt)
        except (TypeError, ValueError):
            logger.warning("[safety_snapshot] failed to parse principal_base_equity", exc_info=True)
            base_eq = None

        if base_eq is None:
            try:
                if self._drawdown_base_equity_usdt is not None and float(self._drawdown_base_equity_usdt) > 0:
                    base_eq = float(self._drawdown_base_equity_usdt)
            except (TypeError, ValueError):
                logger.warning("[safety_snapshot] failed to parse drawdown_base_equity", exc_info=True)
                base_eq = None

        cur_eq = float(self._last_equity_usdt or 0.0)
        dd_pct: Optional[float] = None
        if base_eq is not None and float(base_eq) > 0 and cur_eq > 0:
            try:
                dd_pct = round(max(0.0, (float(base_eq) - float(cur_eq)) / float(base_eq) * 100.0), 4)
            except (TypeError, ValueError, ZeroDivisionError):
                logger.warning("[safety_snapshot] failed to compute drawdown percentage", exc_info=True)
                dd_pct = None

        # Calculate Session PnL — equity-based (unrealized + realized)
        # ★ [2026-05-31 부모] runtime/pnl_baseline.json 우선 사용 (한 클릭 reset 지원).
        #   없으면 DRY_INITIAL_USDT 환경변수 (default 335) fallback.
        session_pnl = 0.0
        try:
            current_equity = float(self._last_equity_usdt or 0.0)
            start_capital = float(os.getenv("DRY_INITIAL_USDT", "335") or 335)
            # runtime/pnl_baseline.json 있으면 우선
            try:
                import json as _json
                _bp = os.path.join("runtime", "pnl_baseline.json")
                if os.path.exists(_bp):
                    with open(_bp, "r", encoding="utf-8") as _f:
                        _baseline_data = _json.load(_f)
                    _file_baseline = float(_baseline_data.get("baseline", 0) or 0)
                    if _file_baseline > 0:
                        start_capital = _file_baseline
            except Exception:
                pass
            if current_equity > 0 and start_capital > 0:
                session_pnl = current_equity - start_capital
        except (TypeError, ValueError) as exc:
            logger.warning("[PNL] equity-based session_pnl failed: %s", exc)

        # ★ [2026-06-02 부모] 전략 가동(enabled) 스냅샷 — v3 좌트리 토글 노브 색(초록=가동중)
        #   FOCUS/HARPOON = config.enabled / plugin = reserved_<name>_enabled AND 슬롯 n>0 (슬롯0=자연정지)
        strat_enabled: Dict[str, bool] = {}
        try:
            _fm = getattr(self, "focus_manager", None)
            strat_enabled["focus"] = bool(getattr(getattr(_fm, "config", None), "enabled", False))
            _hm = getattr(self, "harpoon_manager", None)
            strat_enabled["harpoon"] = bool(getattr(getattr(_hm, "config", None), "enabled", False))
            for _p in ("pingpong", "autoloop", "ladder", "lightning", "gazua", "contrarian", "sniper", "whale"):
                _en = bool(getattr(self, f"reserved_{_p}_enabled", True))
                _n = int(getattr(self, f"reserved_{_p}_n", 0) or 0)
                strat_enabled[_p] = bool(_en and _n > 0)
        except (AttributeError, TypeError, ValueError):
            logger.warning("[status] strategy enabled snapshot failed", exc_info=True)

        return {
            "engine": self.ENGINE_NAME,
            "strategies": strat_enabled,
            "engine_version": getattr(self.engine, "VERSION", "v3"),
            "session_pnl": session_pnl,
            # ★ [2026-05-31 부모] PnL 기산점 명확화 — DRY_INITIAL_USDT env 또는 default 335.
            #   부모님 통찰: "PnL의 기산점이 정확하면 좋은데 사실 그것이 모호..태초부터는 아니겠지"
            "pnl_baseline": start_capital,
            "performance": {
                "tick_duration": self._last_tick_duration,
                "tick_count": self._tick_count,
            },
            "trading_mode": self.trading_mode,
            "emergency_stop": self.emergency_stop,
            "recovery_policy": self.recovery_policy,
            "oma": snap,
            "coordinator": self.coordinator.status(),
            "oma_watch_last_refresh_ts": self.watch_last_refresh_ts,
            "active_prices": prices,
            "ledger": {
                "path": self.ledger.path,
                "run_id": self.ledger.run_id,
            },
            "reconcile": {
                "last_ts": self._last_reconcile_ts,
                "last": dict(self._last_reconcile_result or {}),
                "interval_sec": self.reconcile_interval_sec,
            },
            "equity": {
                "cash_usdt": self._last_cash_usdt,
                "deployed_usdt": self._last_deployed_usdt,
                "equity_usdt": self._last_equity_usdt,
                "ts": self._last_equity_ts,
                "deploy_ratio": self.deploy_ratio,
            },
            "safety": {
                "exit_profit_guard": {
                    "enabled": bool(getattr(self, "exit_profit_guard", False)),
                    "fee_rate": float(getattr(self, "exit_fee_rate", 0.0) or 0.0),
                    "slippage_guard_bps": float(getattr(self, "exit_slippage_guard_bps", 0.0) or 0.0),
                    "min_net_profit_pct": float(getattr(self, "exit_min_net_profit_pct", 0.0) or 0.0),
                    "min_net_profit_usdt": float(getattr(self, "exit_min_net_profit_usdt", 0.0) or 0.0),
                    "streak_n": int(getattr(self, "exit_profit_guard_streak_n", 0) or 0),
                    "streak_window_sec": float(getattr(self, "exit_profit_guard_streak_window_sec", 0.0) or 0.0),
                    "streak_cooldown_sec": float(getattr(self, "exit_profit_guard_streak_cooldown_sec", 0.0) or 0.0),
                    "streak_to_recovery": bool(getattr(self, "exit_profit_guard_streak_to_recovery", False)),
                    "streak_notify": bool(getattr(self, "exit_profit_guard_streak_notify", False)),
                },
                "global_entry_cooldown": {
                    "until_ts": float(getattr(self, "_global_entry_block_until_ts", 0.0) or 0.0),
                    "remaining_sec": float(self._cooldown_remaining(getattr(self, "_global_entry_block_until_ts", 0.0) or 0.0)),
                    "reason": str(getattr(self, "_global_entry_block_reason", "") or ""),
                },
                "drawdown_guard": {
                    "enabled": bool(getattr(self, "drawdown_guard", False)),
                    "action": str(getattr(self, "drawdown_action", "") or ""),
                    "threshold_pct": float(getattr(self, "max_drawdown_pct", 0.0) or 0.0),
                    "base_equity_usdt": base_eq,
                    "equity_usdt": cur_eq,
                    "drawdown_pct": dd_pct,
                    "latched": bool(getattr(self, "_drawdown_latched", False)),
                    "cooldown_sec": float(getattr(self, "drawdown_cooldown_sec", 0.0) or 0.0),
                },
            },
            "triage": (
                self.triage_manager.get_status_dict()
                if getattr(self, "triage_manager", None) is not None
                else {"state": "NORMAL", "active": False}
            ),
        }
