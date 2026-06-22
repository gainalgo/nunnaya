# ============================================================
# File: app/core/hs_mixin_budget.py
# Phase 5E: Budget allocation methods extracted from hyper_system.py
# ============================================================

from __future__ import annotations

import logging
import math
import os
import time
from typing import Any, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    pass

from app.core.hyper_price_store import price_store

logger = logging.getLogger(__name__)


class BudgetMixin:
    """Budget allocation, smart allocation, and rebalancing mixin."""

    def _rebalance_allocations(self, *, active_markets: List[str]) -> None:
        """ACTIVE 시장에 대한 soft allocation.

        목적(이번 이슈의 핵심):
        - 한 번 매수/매도 후에도 주문 금액이 0 근처로 떨어지지 않게 해서 pingpong이 연속 동작하도록 함.
        - "원금(stake)"과 "수익"을 분리:
          * OMA_FIXED_PRINCIPAL=True  → stake(원금)는 고정, 수익은 stake에 합산하지 않음(복리 미적용)
          * OMA_FIXED_PRINCIPAL=False → 기존 방식(현재 deployed를 제외한 "추가 배치 가능 금액"만 분배)

        안전장치:
        - 손실로 equity가 감소하면(=target_deployed가 감소하면) stake 총액은 자동으로 축소될 수 있음.
        - LIVE에서도 engine sizing이 stake를 정확히 보도록 usable_capital을 함께 갱신한다.
        """
        with self._lock:
            if not active_markets:
                return

            markets = list(dict.fromkeys([m for m in active_markets if m]))
            if not markets:
                return

            equity = 0.0
            cash = 0.0
            deployed = 0.0

            # PAPER fallback / LIVE snapshot
            if self.trading_mode == "PAPER":
                try:
                    if self.trade_client and hasattr(self.trade_client, "get_balance"):
                        # PaperTradeClient — 실시간 가상 잔고 반영
                        equity_usdt = float(self.trade_client.get_balance("USDT"))
                        # 코인 포지션 가치 합산
                        coin_value = 0.0
                        if hasattr(self.trade_client, "_coin_balances"):
                            from app.core.hyper_price_store import price_store
                            for coin, qty in self.trade_client._coin_balances.items():
                                p = price_store.get_price(coin + "USDT") or 0.0
                                coin_value += qty * p
                        equity = equity_usdt + coin_value
                        cash = equity_usdt
                        deployed = coin_value
                    else:
                        equity = float(os.getenv("DRY_INITIAL_USDT", "1000"))
                        cash = equity
                        deployed = 0.0
                except (TypeError, ValueError):
                    logger.warning("[Budget] Paper balance error, using fallback 1000")
                    equity = 1000.0
                    cash = equity
                    deployed = 0.0
            else:
                equity = float(self._last_equity_usdt or 0.0)
                cash = float(self._last_cash_usdt or 0.0)
                deployed = float(self._last_deployed_usdt or 0.0)

                if equity <= 0:
                    # equity 추정 불가 시에는 cash만 기준
                    equity = cash

            if equity <= 0:
                return

            # FOCUS 예산 격리: FOCUS가 활성화되어 있으면 해당 예산을 equity에서 차감
            try:
                fm = getattr(self, "focus_manager", None)
                if fm and getattr(fm, "enabled", False):
                    _focus_fixed = float(fm.budget_usdt or 0)
                    if _focus_fixed > 0:
                        equity = max(0.0, equity - _focus_fixed)
                    else:
                        # Budget=0 자동 모드: FOCUS 실제 배치 자본 차감
                        try:
                            from app.core.cross_strategy_guard import get_total_deployed_usdt
                            _focus_deployed, _ = get_total_deployed_usdt(self)
                            if _focus_deployed > 0:
                                equity = max(0.0, equity - _focus_deployed)
                        except Exception:
                            pass
            except Exception:
                pass

            # deploy_ratio 적용 후 소숫점 2자리 버림 (USDT 기준)
            target_deployed = max(
                0.0,
                float(int(float(equity) * float(self.deploy_ratio) * 100) / 100),
            )

            # --- capital mode selection ---
            capital_mode = "INCREMENTAL"
            principal_total = 0.0
            principal_effective = 0.0

            if bool(getattr(self, "fixed_principal", False)):
                capital_mode = "FIXED_PRINCIPAL"

                # 최초 1회: 기준 stake 총액을 고정 (이익은 여기에 합산하지 않음)
                # target_deployed > 0 일 때만 설정 (0이면 의미 없음)
                if self._principal_total_usdt is None and float(target_deployed) > 0:
                    self._principal_base_equity_usdt = float(equity)
                    self._principal_total_usdt = float(target_deployed)

                    self.ledger.append(
                        "PRINCIPAL_BASE_SET",
                        base_equity_usdt=float(equity),
                        principal_total_usdt=float(self._principal_total_usdt),
                        deploy_ratio=float(self.deploy_ratio),
                    )

                principal_total = float(self._principal_total_usdt or 0.0)

                # 손실로 equity가 감소하면 stake 총액은 축소(안전)
                principal_effective = float(min(principal_total, float(target_deployed)))
                deployable = float(principal_effective)

                # 관측용(legacy 뷰 호환)
                additional_budget = max(0.0, float(target_deployed) - max(0.0, float(deployed)))
            else:
                additional_budget = max(0.0, float(target_deployed) - max(0.0, float(deployed)))
                deployable = min(float(additional_budget), max(0.0, float(cash)))

            # ------------------------------------------------------------
            # MANUAL BUDGET (hard-lock)
            # - budget_usdt가 설정된 마켓은 자동 분배로 덮어쓰지 않는다.
            # - 총 수동예산이 deployable을 초과하면 비율 스케일링으로 축소 적용한다.
            manual_budgets: Dict[str, float] = {}
            manual_budget_n = 0
            manual_budget_total_usdt = 0.0
            manual_budget_scale = 1.0
            manual_budget_scaled_total_usdt = 0.0
            remaining_deployable_usdt = float(deployable)
            auto_markets = list(markets)
            auto_n = len(auto_markets)

            # [FIX] Bybit: get_budget_usdt 사용
            get_budget_fn = getattr(self.oma_registry, "get_budget_usdt", None)
            if not callable(get_budget_fn):
                get_budget_fn = getattr(self.oma_registry, "get_budget_usdt", None)
            if callable(get_budget_fn):
                for m in markets:
                    b = get_budget_fn(m)
                    if b is None:
                        continue
                    try:
                        b_f = float(b)
                    except (TypeError, ValueError) as exc:
                        logger.warning("[BUDGET] get_budget_usdt parse error for %s: %s", m, exc, exc_info=True)
                        continue
                    if b_f > 0:
                        manual_budgets[m] = b_f

                manual_budget_n = len(manual_budgets)
                manual_budget_total_usdt = float(sum(manual_budgets.values()))
                if manual_budget_total_usdt > 0 and float(deployable) > 0 and manual_budget_total_usdt > float(deployable):
                    manual_budget_scale = float(deployable) / manual_budget_total_usdt

                manual_budget_scaled_total_usdt = manual_budget_total_usdt * manual_budget_scale

                # 먼저 수동 예산 적용
                for m, b_f in manual_budgets.items():
                    ctx = self.coordinator.ensure_market(m)
                    scaled = float(b_f) * manual_budget_scale
                    ctx.allocated_capital = scaled
                    ctx.usable_capital = scaled
                    ctx.wallet_mode = bool(self.wallet_mode)
                    # PATCH 2025-12-26: wallet-mode => do NOT top-up usable_capital
                    if self.wallet_mode:
                        if float(getattr(ctx, 'usable_capital', 0.0) or 0.0) <= 0.0:
                            ctx.usable_capital = float(scaled)
                        else:
                            ctx.usable_capital = min(float(ctx.usable_capital), float(scaled))
                    else:
                        ctx.usable_capital = float(scaled)

                remaining_deployable_usdt = max(0.0, float(deployable) - float(manual_budget_scaled_total_usdt))
                auto_markets = [m for m in markets if m not in manual_budgets]
                auto_n = len(auto_markets)
            # ------------------------------------------------------------

            # allocation 대상(시장) 선정: ACTIVE 전체를 기본 대상
            # 전략 버킷 구성
            buckets: Dict[str, List[str]] = {}
            for m in auto_markets:
                ctx = self.coordinator.ensure_market(m)
                strat = str(getattr(ctx, "selected_strategy", None) or getattr(ctx, "bias", None) or "UNKNOWN").upper()
                buckets.setdefault(strat, []).append(m)

            # 전략 버킷 가중치 적용(선택)
            weights: Dict[str, float] = {}
            try:
                if isinstance(self.strategy_bucket_weights, dict):
                    for k, v in self.strategy_bucket_weights.items():
                        try:
                            weights[str(k).upper()] = float(v)
                        except (TypeError, ValueError) as exc:
                            logger.warning("[BUDGET] strategy bucket weight parse error for %s: %s", k, exc, exc_info=True)
                            continue
            except AttributeError:
                logger.warning("[Budget] strategy_bucket_weights not available", exc_info=True)
                weights = {}

            # 없으면 균등
            if not weights:
                for k in buckets.keys():
                    weights[k] = 1.0

            wsum = sum(weights.values()) if weights else 0.0
            if wsum <= 0:
                wsum = 1.0

            # 버킷 예산 → 코인 분배 (Smart Allocation 적용)
            for strat, mks in buckets.items():
                w = float(weights.get(strat, 0.0))
                if w <= 0:
                    continue

                bucket_budget = float(remaining_deployable_usdt) * (w / wsum)

                if not mks:
                    continue

                # Smart Allocation: AI/수익 기반 분배
                if self.smart_alloc_enabled and len(mks) > 1:
                    allocations = self._smart_allocate_bucket(bucket_budget, mks)
                else:
                    # 균등 분배 fallback
                    per_market = bucket_budget / float(len(mks))
                    allocations = {m: per_market for m in mks}

                for m, alloc in allocations.items():
                    ctx = self.coordinator.ensure_market(m)
                    ctx.allocated_capital = float(alloc)
                    ctx.wallet_mode = bool(self.wallet_mode)
                    # LIVE에서도 engine sizing이 stake를 제대로 보도록 같이 갱신
                    # PATCH 2025-12-26: wallet-mode => do NOT top-up usable_capital
                    if self.wallet_mode:
                        if float(getattr(ctx, 'usable_capital', 0.0) or 0.0) <= 0.0:
                            ctx.usable_capital = float(alloc)
                        else:
                            ctx.usable_capital = min(float(ctx.usable_capital), float(alloc))
                    else:
                        ctx.usable_capital = float(alloc)

            # UI/관측용(스팸 방지: 주기/변화 기반)
            try:
                sig = f"{capital_mode}|{len(markets)}|{round(deployable,2)}|{round(equity,2)}|{round(deployed,2)}|{round(target_deployed,2)}|MB{round(manual_budget_scaled_total_usdt,2)}|A{auto_n}"
            except (TypeError, ValueError):
                logger.warning("[Budget] alloc sig format error", exc_info=True)
                sig = ""

            now = time.time()
            # [2026-03-09] 로그 빈도 완화: 300초 주기 OR sig 변경 + 최소 60초 간격
            _alloc_elapsed = now - float(self._last_alloc_log_ts or 0.0)
            _sig_changed = bool(sig and sig != self._last_alloc_sig)
            if _alloc_elapsed >= 300.0 or (_sig_changed and _alloc_elapsed >= 60.0):
                self.ledger.append(
                    "ALLOC_REBALANCE",
                    capital_mode=capital_mode,
                    equity_usdt=equity,
                    cash_usdt=cash,
                    deployed_usdt=deployed,
                    target_deployed_usdt=target_deployed,
                    additional_budget_usdt=additional_budget,
                    deployable_usdt=deployable,
                    principal_total_usdt=principal_total,
                    principal_effective_usdt=principal_effective,
                    active_n=len(markets),
                    buckets=list(buckets.keys()),
                    weights=weights,
                    manual_budget_n=manual_budget_n,
                    manual_budget_total_usdt=manual_budget_total_usdt,
                    manual_budget_scaled_total_usdt=manual_budget_scaled_total_usdt,
                    manual_budget_scale=manual_budget_scale,
                    remaining_deployable_usdt=remaining_deployable_usdt,
                    auto_n=auto_n,
                )
                self._last_alloc_log_ts = now
                self._last_alloc_sig = sig

    def _smart_allocate_bucket(self, bucket_budget: float, markets: List[str]) -> Dict[str, float]:
        """Smart Allocation: AI/수익/모멘텀/Kelly 기반 버킷 내부 분배.

        1단계: 코인별 스코어 계산 (수익률, AI, 리스크, 모멘텀, Kelly)
        2단계: 스코어 기반 초기 weight 산출
        3단계: 상관관계 패널티 적용 (post-adjust)
        4단계: 섹터 밸런싱 적용 (post-adjust)
        """
        import math

        if not markets:
            return {}

        n = len(markets)
        equal_share = bucket_budget / n
        now = time.time()
        lookback_sec = self.smart_alloc_lookback_days * 86400

        # ================================================================
        # 1단계: 코인별 스코어 계산
        # ================================================================
        score_details: Dict[str, Dict[str, float]] = {}
        price_returns: Dict[str, List[float]] = {}  # 상관관계 계산용

        for m in markets:
            detail = {"profit": 0.0, "ai": 0.0, "risk": 0.0, "momentum": 0.0, "kelly": 0.0}

            try:
                ctx = self.coordinator.contexts.get(m)
                if not ctx:
                    score_details[m] = detail
                    continue

                # 웜업 미완료면 균등
                if hasattr(ctx, 'is_ready') and callable(ctx.is_ready) and not ctx.is_ready():
                    score_details[m] = detail
                    continue

                brain = getattr(ctx, 'current_ai', None)
                trade_history = getattr(ctx, 'trade_history', None) or []

                # --- 수익률 컴포넌트 ---
                try:
                    if trade_history:
                        recent_pnl = 0.0
                        for rec in trade_history:
                            ts = rec[0] if isinstance(rec, (list, tuple)) else rec.get('ts', 0)
                            pnl = rec[1] if isinstance(rec, (list, tuple)) else rec.get('pnl', 0)
                            if ts >= now - lookback_sec:
                                recent_pnl += float(pnl)

                        base_cap = max(float(ctx.allocated_capital or equal_share), 1.0)
                        roi = recent_pnl / base_cap
                        detail["profit"] = math.tanh(roi / 0.05)
                except (AttributeError, IndexError, TypeError, ValueError) as exc:
                    logger.warning("[BUDGET] profit component error for %s: %s", m, exc, exc_info=True)

                # --- AI 신뢰도 컴포넌트 ---
                try:
                    ai_conf = None
                    if isinstance(brain, dict) and 'brain' in brain:
                        ai_conf = float((brain.get('brain') or {}).get('ai_confidence', 0.5))
                    elif hasattr(ctx, 'confidence'):
                        ai_conf = min(1.0, float(ctx.confidence or 0) / 10.0)

                    if ai_conf is not None:
                        detail["ai"] = (ai_conf - 0.5) * 2
                except (AttributeError, KeyError, TypeError, ValueError) as exc:
                    logger.warning("[BUDGET] AI confidence component error for %s: %s", m, exc, exc_info=True)

                # --- 리스크 컴포넌트 ---
                try:
                    vol = 0.0
                    if isinstance(brain, dict) and 'brain' in brain:
                        vol = float((brain.get('brain') or {}).get('volatility', 0))

                    risk_val = 0.0
                    if self.smart_alloc_vol_th > 0 and vol > self.smart_alloc_vol_th:
                        risk_val += (vol - self.smart_alloc_vol_th) / self.smart_alloc_vol_th
                    if detail["profit"] < 0:
                        risk_val += self.smart_alloc_loss_penalty
                    detail["risk"] = min(risk_val, 2.0)
                except (AttributeError, KeyError, TypeError, ValueError) as exc:
                    logger.warning("[BUDGET] risk component error for %s: %s", m, exc, exc_info=True)

                # --- 모멘텀 컴포넌트 ---
                try:
                    price_history = getattr(ctx, 'price_history', None) or []
                    if not price_history and isinstance(brain, dict) and 'brain' in brain:
                        price_history = (brain.get('brain') or {}).get('price_history', [])

                    mom_lookback = self.smart_alloc_mom_lookback
                    if price_history and len(price_history) >= 2:
                        prices = [float(p) for p in price_history[-mom_lookback:] if p]
                        if len(prices) >= 2:
                            # log returns
                            returns = []
                            for i in range(1, len(prices)):
                                if prices[i-1] > 0:
                                    returns.append(math.log(prices[i] / prices[i-1]))

                            if returns:
                                price_returns[m] = returns  # 상관관계용 저장
                                mean_ret = sum(returns) / len(returns)
                                std_ret = (sum((r - mean_ret)**2 for r in returns) / len(returns)) ** 0.5

                                if std_ret > 1e-10:
                                    mom_raw = mean_ret / std_ret
                                    # sigmoid 정규화 (0~1)
                                    detail["momentum"] = 1.0 / (1.0 + math.exp(-mom_raw / self.smart_alloc_mom_scale))
                                else:
                                    detail["momentum"] = 0.5  # 변동 없음 = 중립
                except (AttributeError, KeyError, OverflowError, TypeError, ValueError, ZeroDivisionError) as exc:
                    logger.warning("[BUDGET] momentum component error for %s: %s", m, exc, exc_info=True)

                # --- Kelly Criterion 컴포넌트 ---
                try:
                    if trade_history and len(trade_history) >= self.smart_alloc_kelly_min_trades:
                        wins = []
                        losses = []
                        for rec in trade_history[-100:]:  # 최근 100개
                            pnl = rec[1] if isinstance(rec, (list, tuple)) else rec.get('pnl', 0)
                            pnl = float(pnl)
                            if pnl > 0:
                                wins.append(pnl)
                            elif pnl < 0:
                                losses.append(abs(pnl))

                        total_trades = len(wins) + len(losses)
                        if total_trades >= self.smart_alloc_kelly_min_trades and losses:
                            p = len(wins) / total_trades  # 승률
                            avg_win = sum(wins) / len(wins) if wins else 0
                            avg_loss = sum(losses) / len(losses) if losses else 1
                            b = avg_win / avg_loss if avg_loss > 0 else 1  # 평균이익/평균손실

                            # Kelly formula: f* = (p*(b+1) - 1) / b
                            if b > 0:
                                kelly_f = (p * (b + 1) - 1) / b
                                kelly_f = max(0, min(kelly_f, self.smart_alloc_kelly_max))
                                kelly_f *= self.smart_alloc_kelly_frac  # fractional Kelly
                                detail["kelly"] = kelly_f / self.smart_alloc_kelly_max  # 0~1 정규화
                except (AttributeError, IndexError, TypeError, ValueError, ZeroDivisionError) as exc:
                    logger.warning("[BUDGET] Kelly criterion error for %s: %s", m, exc, exc_info=True)

                # --- 유동성 컴포넌트 (거래대금 기반) ---
                try:
                    vol_24h = 0.0
                    # price_store에서 거래대금 조회
                    vol_24h = price_store.get_volume(m) or 0.0

                    if vol_24h <= 0:
                        # ctx에서 조회 시도
                        if isinstance(brain, dict) and 'brain' in brain:
                            vol_24h = float(brain['brain'].get('vol24_usdt', brain['brain'].get('vol24_usdt', 0)) or 0)

                    if vol_24h > 0:
                        # 24h 거래대금 기준 정규화 (0~1, log scale)
                        # 1M=0, 10M=0.17, 100M=0.33, 1B=0.5, 10B=0.67
                        log_vol = math.log10(max(vol_24h, 1e6)) - 6  # 1M = 0
                        detail["liquidity"] = min(1.0, max(0.0, log_vol / 6))  # 0~1 (1T USDT = 1.0)
                    else:
                        detail["liquidity"] = 0.5  # 정보 없으면 중립
                except (KeyError, AttributeError, TypeError, ValueError):
                    logger.warning("[Budget] liquidity score error for %s", m, exc_info=True)
                    detail["liquidity"] = 0.5

                # --- 고래 활동 감지 ---
                # 2026-03-10: 실제 구현체 연결
                try:
                    from app.monitor.whale_detector import get_whale_detector
                    whale_det = get_whale_detector()

                    # 거래량 데이터
                    vol_24h = price_store.get_volume(m) or 0
                    avg_vol = equal_share  # 임시 기준 (실제는 평균 거래량 필요)

                    # 가격 변화
                    price_change_pct = 0.0
                    if price_history and len(price_history) >= 2:
                        p_now = float(price_history[-1])
                        p_prev = float(price_history[-24]) if len(price_history) >= 24 else float(price_history[0])
                        if p_prev > 0:
                            price_change_pct = ((p_now - p_prev) / p_prev) * 100

                    whale_info = whale_det.detect(vol_24h, avg_vol * 1000, price_change_pct, market=m)
                    detail["whale_mult"] = whale_det.get_budget_weight(whale_info)
                    detail["whale_signal"] = whale_info.signal.value
                except (KeyError, IndexError, AttributeError, TypeError, ValueError):
                    logger.warning("[Budget] whale detection error for %s", m, exc_info=True)
                    detail["whale_mult"] = 1.0

                # --- 이벤트 감지 ---
                # [2026-03-15] EventDetector 미구현 — 클래스 자체가 없어 매 틱 ImportError.
                # 구현 시까지 중립값(1.0) 고정. 구현 후 이 블록을 활성화할 것.
                detail["event_mult"] = 1.0

                score_details[m] = detail

            except Exception:
                logger.warning("[Budget] smart_allocate_bucket unexpected error for %s", m, exc_info=True)
                score_details[m] = detail

        # ================================================================
        # 2단계: 가중합으로 raw_score 계산
        # ================================================================
        # 가중치 정규화: w_profit+w_ai+w_risk+w_momentum+w_kelly+w_liquidity 합이 1.0이 되도록
        _w_sum = (
            self.smart_alloc_w_profit + self.smart_alloc_w_ai + self.smart_alloc_w_risk
            + self.smart_alloc_w_momentum + self.smart_alloc_w_kelly + self.smart_alloc_w_liquidity
        )
        _w_norm = _w_sum if _w_sum > 0 else 1.0
        _wp = self.smart_alloc_w_profit / _w_norm
        _wa = self.smart_alloc_w_ai / _w_norm
        _wr = self.smart_alloc_w_risk / _w_norm
        _wm = self.smart_alloc_w_momentum / _w_norm
        _wk = self.smart_alloc_w_kelly / _w_norm
        _wl = self.smart_alloc_w_liquidity / _w_norm

        scores: Dict[str, float] = {}
        for m in markets:
            d = score_details.get(m, {})
            raw_score = 1.0
            raw_score += _wp * d.get("profit", 0)
            raw_score += _wa * d.get("ai", 0)
            raw_score -= _wr * d.get("risk", 0)
            raw_score += _wm * (d.get("momentum", 0.5) - 0.5) * 2  # 0.5 = 중립
            raw_score += _wk * d.get("kelly", 0)
            raw_score += _wl * (d.get("liquidity", 0.5) - 0.5) * 2  # 유동성 가중치

            # 고래/이벤트 가중치 적용
            whale_mult = d.get("whale_mult", 1.0)
            event_mult = d.get("event_mult", 1.0)
            raw_score *= whale_mult * event_mult

            scores[m] = max(raw_score, 0.1)

        # 초기 weight 계산
        total_score = sum(scores.values())
        if total_score <= 0:
            total_score = n

        weights: Dict[str, float] = {}
        for m in markets:
            weights[m] = scores[m] / total_score

        # ================================================================
        # 3단계: 상관관계 패널티 (post-adjust)
        # ================================================================
        if self.smart_alloc_corr_enabled and len(price_returns) >= 2:
            try:
                # 간단한 피어슨 상관계수 계산
                def pearson_corr(a: List[float], b: List[float]) -> float:
                    n_min = min(len(a), len(b))
                    if n_min < 5:
                        return 0.0
                    a, b = a[-n_min:], b[-n_min:]
                    mean_a = sum(a) / n_min
                    mean_b = sum(b) / n_min
                    cov = sum((a[i] - mean_a) * (b[i] - mean_b) for i in range(n_min)) / n_min
                    std_a = (sum((x - mean_a)**2 for x in a) / n_min) ** 0.5
                    std_b = (sum((x - mean_b)**2 for x in b) / n_min) ** 0.5
                    if std_a > 1e-10 and std_b > 1e-10:
                        return cov / (std_a * std_b)
                    return 0.0

                # 각 코인의 상관 노출도 계산
                corr_exposure: Dict[str, float] = {}
                for m in markets:
                    if m not in price_returns:
                        corr_exposure[m] = 0.0
                        continue

                    exposure = 0.0
                    for n_other in markets:
                        if n_other == m or n_other not in price_returns:
                            continue
                        corr = pearson_corr(price_returns[m], price_returns[n_other])
                        if corr > self.smart_alloc_corr_th:
                            exposure += (corr - self.smart_alloc_corr_th) * weights.get(n_other, 0)

                    corr_exposure[m] = exposure

                # 패널티 적용
                for m in markets:
                    penalty = 1.0 / (1.0 + self.smart_alloc_corr_lambda * corr_exposure.get(m, 0))
                    weights[m] *= penalty

            except (AttributeError, KeyError, TypeError, ValueError, ZeroDivisionError) as exc:
                logger.warning("[BUDGET] correlation penalty error: %s", exc, exc_info=True)

        # ================================================================
        # 4단계: 섹터 밸런싱 (post-adjust)
        # ================================================================
        if self.smart_alloc_sector_enabled and self.smart_alloc_sector_map:
            try:
                sector_map = self.smart_alloc_sector_map
                sector_caps = self.smart_alloc_sector_caps or {}
                default_cap = self.smart_alloc_sector_default_cap

                # 섹터별 현재 비중 계산
                sector_weights: Dict[str, float] = {}
                for m in markets:
                    sector = sector_map.get(m, "OTHERS")
                    sector_weights[sector] = sector_weights.get(sector, 0) + weights.get(m, 0)

                # 섹터 캡 초과 시 패널티
                for m in markets:
                    sector = sector_map.get(m, "OTHERS")
                    cap = float(sector_caps.get(sector, default_cap))
                    current_sector_w = sector_weights.get(sector, 0)

                    if current_sector_w > cap and current_sector_w > 0:
                        penalty = cap / current_sector_w
                        weights[m] *= penalty

            except (AttributeError, KeyError, TypeError, ValueError, ZeroDivisionError) as exc:
                logger.warning("[BUDGET] sector balancing error: %s", exc, exc_info=True)

        # ================================================================
        # 최종: 정규화 및 예산 분배
        # ================================================================
        total_weight = sum(weights.values())
        if total_weight <= 0:
            total_weight = 1.0

        # min/max 캡 적용
        min_cap = equal_share * self.smart_alloc_min_mult
        max_cap = equal_share * self.smart_alloc_max_mult

        # ================================================================
        # 예산 배율 전략 적용 (OMA_BUDGET_STRATEGY)
        # ================================================================
        # - regime  = 기존 추세 추종 (BULL→x1.25, BEAR→x0.70)
        # - fg      = F&G 역발상 (공포→x1.30, 탐욕→x0.70)
        # - extreme = F&G 극단값(0-25, 75-100)만, 나머지는 Regime (기본값)
        # - hybrid  = F&G × Regime 곱연산
        # ================================================================
        budget_mult = 1.0
        budget_reason = "none"

        # F&G 정보 조회
        fg_mult = 1.0
        fg_level = None
        fg_value = 50
        if self.fear_greed_enabled:
            try:
                from app.core.fear_greed import get_fear_greed_index, FearGreedLevel
                fg = get_fear_greed_index()
                info = fg.get_index()
                fg_mult = info.budget_mult
                fg_level = info.level
                fg_value = info.value
            except (ImportError, AttributeError, TypeError) as exc:
                logger.warning("[BUDGET] Fear&Greed lookup error: %s", exc, exc_info=True)

        # Regime 정보 조회
        regime_mult = 1.0
        global_regime = None
        if self.regime_enabled:
            try:
                from app.core.market_regime import RegimeDetector, MarketRegime
                detector = getattr(self, '_regime_detector', None)
                if detector is None:
                    detector = RegimeDetector()
                    self._regime_detector = detector

                global_regime = detector.get_global_regime(self.coordinator.contexts)

                if global_regime.regime == MarketRegime.BULL:
                    regime_mult = self.regime_bull_max_mult_x
                elif global_regime.regime == MarketRegime.BEAR:
                    regime_mult = self.regime_bear_max_mult_x
            except (KeyError, AttributeError, TypeError) as exc:
                logger.warning("[BUDGET] Regime lookup error: %s", exc, exc_info=True)

        # 전략별 적용
        strategy = getattr(self, 'budget_strategy', 'extreme')

        if strategy == "regime":
            # 기존 추세 추종만
            budget_mult = regime_mult
            budget_reason = f"regime:{global_regime.regime.value if global_regime else 'UNKNOWN'}→{regime_mult:.2f}"

        elif strategy == "fg":
            # F&G 역발상만
            budget_mult = fg_mult
            budget_reason = f"fg:{fg_value}→{fg_mult:.2f}"

        elif strategy == "extreme":
            # F&G 극단값(0-25, 75-100)만, 나머지는 Regime
            try:
                from app.core.fear_greed import FearGreedLevel
                if fg_level in (FearGreedLevel.EXTREME_FEAR, FearGreedLevel.EXTREME_GREED):
                    budget_mult = fg_mult
                    budget_reason = f"extreme_fg:{fg_value}→{fg_mult:.2f}"
                else:
                    budget_mult = regime_mult
                    budget_reason = f"extreme_regime:{global_regime.regime.value if global_regime else 'UNKNOWN'}→{regime_mult:.2f}"
            except (ImportError, AttributeError, NameError):
                logger.warning("[Budget] FearGreedLevel import failed, using regime fallback", exc_info=True)
                budget_mult = regime_mult
                budget_reason = f"extreme_fallback→{regime_mult:.2f}"

        elif strategy == "hybrid":
            # F&G × Regime 곱연산
            budget_mult = fg_mult * regime_mult
            budget_reason = f"hybrid:fg{fg_mult:.2f}×regime{regime_mult:.2f}={budget_mult:.2f}"

        else:
            # 알 수 없는 전략 → 기본값 (extreme)
            budget_mult = regime_mult
            budget_reason = f"unknown_strategy→regime:{regime_mult:.2f}"

        max_cap *= budget_mult
        min_cap *= budget_mult

        # 시간대 최적화 적용
        # 2026-03-10: 실제 구현체(time_volatility_adjuster.py)로 연결
        # 옵션: system.time_zone_optimizer_enabled (기본 False)
        if bool(getattr(self, "time_zone_optimizer_enabled", False)):
            try:
                from app.monitor.time_volatility_adjuster import get_time_volatility_adjuster
                _tva = get_time_volatility_adjuster()
                time_mult = _tva.get_volatility_multiplier()
                max_cap *= time_mult
                min_cap *= time_mult
            except (ImportError, AttributeError, TypeError) as exc:
                logger.warning("[BUDGET] time zone optimizer error: %s", exc, exc_info=True)

        allocations: Dict[str, float] = {}
        for m in markets:
            raw_alloc = bucket_budget * (weights[m] / total_weight)

            # 유동성 기반 예산 상한 (slippage 방지)
            # 24시간 거래대금의 일정 비율 이상 배정하지 않음
            # NOTE: 수동 예산(GAZUA 등)은 이 함수에 들어오기 전에 이미 처리됨
            #       → 유동성 상한 적용 안됨 (사용자 의도 존중)
            liquidity_cap = max_cap
            try:
                d = score_details.get(m, {})
                liq_score = d.get("liquidity", 0.5)
                # 유동성 점수 → 대략적인 거래대금 역산
                # liq_score 0.5 = 10억, 0.7 = 100억
                estimated_vol24 = 10 ** (6 + liq_score * 6)  # 1M ~ 1T
                # 거래대금의 0.1% 이상 배정하지 않음 (슬리피지 방지)
                liquidity_cap = min(max_cap, estimated_vol24 * self.smart_alloc_liq_cap_ratio)
            except (AttributeError, TypeError, ValueError) as exc:
                logger.warning("[BUDGET] liquidity cap error for %s: %s", m, exc, exc_info=True)

            capped = max(min_cap, min(max_cap, min(liquidity_cap, raw_alloc)))
            allocations[m] = float(int(capped * 100) / 100)  # USDT 0.01 단위

        # 합계 조정 (라운딩 오차 보정)
        total_alloc = sum(allocations.values())
        diff = bucket_budget - total_alloc
        if abs(diff) > 0.1 and allocations:
            max_m = max(allocations, key=lambda x: allocations[x])
            allocations[max_m] = max(0, allocations[max_m] + diff)

        return allocations

    def _cooldown_remaining(self, until_ts: Optional[float]) -> float:
        if not until_ts:
            return 0.0
        try:
            return max(0.0, float(until_ts) - time.time())
        except (TypeError, ValueError) as exc:
            try:
                self.ledger.append("SYSTEM_HELPER_ERROR", where="cooldown_remaining", error=str(exc))
            except (AttributeError, TypeError, ValueError) as exc:
                logger.warning("[BUDGET] cooldown_remaining ledger fallback: %s", exc, exc_info=True)
            return 0.0

    # --------------------------------------------------------
    # Dynamic Budget Rebalancing
    # --------------------------------------------------------
    def check_and_rebalance_budgets(self, *, force: bool = False) -> Dict[str, Any]:
        """자본 변동 시 예산 자동 리밸런싱.

        Args:
            force: True면 변동률 무관하게 강제 리밸런싱

        Returns:
            리밸런싱 결과 (변동 사항)
        """
        result: Dict[str, Any] = {"rebalanced": False, "reason": "no_change"}

        try:
            old_equity = float(getattr(self, "_prev_rebalance_equity", 0.0) or 0.0)
            new_equity = float(self._last_equity_usdt or 0.0)

            if new_equity <= 0:
                result["reason"] = "no_equity"
                return result

            # 변동률 계산
            if old_equity > 0:
                change_ratio = new_equity / old_equity
            else:
                change_ratio = 1.0
                self._prev_rebalance_equity = new_equity

            # 5% 이상 변동 시 리밸런싱 (또는 force=True)
            rebalance_threshold = float(getattr(self, "budget_rebalance_threshold", 0.05) or 0.05)

            if not force and abs(change_ratio - 1.0) < rebalance_threshold:
                result["reason"] = f"change_under_threshold ({abs(change_ratio - 1.0) * 100:.2f}% < {rebalance_threshold * 100}%)"
                return result

            # 리밸런싱 실행
            snap = self.oma_registry.snapshot() if self.oma_registry else {}
            active_markets = [r.get("market") if isinstance(r, dict) else r for r in (snap.get("active") or [])]

            if not active_markets:
                result["reason"] = "no_active_markets"
                return result

            # 예산 재배분 트리거
            self._allocate_capital_to_markets(active_markets)

            # 상태 업데이트
            self._prev_rebalance_equity = new_equity

            result = {
                "rebalanced": True,
                "reason": "capital_changed" if not force else "forced",
                "old_equity": old_equity,
                "new_equity": new_equity,
                "change_pct": round((change_ratio - 1.0) * 100, 2),
                "markets_count": len(active_markets),
            }

            self.ledger.append(
                "BUDGET_REBALANCED",
                old_equity=old_equity,
                new_equity=new_equity,
                change_pct=result["change_pct"],
                markets=len(active_markets),
                reason=result["reason"],
            )

        except (KeyError, AttributeError, TypeError, ValueError) as exc:
            result = {"rebalanced": False, "reason": "error", "error": str(exc)}
            try:
                self.ledger.append("BUDGET_REBALANCE_ERROR", error=str(exc))
            except (AttributeError, TypeError, ValueError) as exc:
                logger.warning("[BUDGET] 상태 업데이트: %s", exc, exc_info=True)

        return result
