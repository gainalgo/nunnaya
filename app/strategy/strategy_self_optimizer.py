# ============================================================
# File: app/strategy/strategy_self_optimizer.py
# ------------------------------------------------------------
# StrategySelfOptimizer
# - Risk 조정 이후 신호를 기반으로 장기 전략 품질을 향상시키는 레이어.
# - 현재는 경량 형태이지만 구조적으로 확장 가능.
# ============================================================

from __future__ import annotations
from typing import Dict, Any
import time

from .strategy_types import (
    StrategyPolicy,
    StrategySignal,
    StrategyBrainOutput,
)


class StrategySelfOptimizer:
    """
    장기적 전략 튜닝을 위한 경량 최적화 모듈.
    지금은 단순 pass-through 형태이지만
    구조적으로 확장할 수 있도록 남겨둔 레이어이다.
    """

    # --------------------------------------------------------
    # 최적화 메인 함수
    # --------------------------------------------------------
    def refine(
        self,
        market: str,
        price: float,
        policy: StrategyPolicy,
        brain: StrategyBrainOutput,
        signal: StrategySignal,
        context: Any = None
    ) -> StrategySignal:
        """
        승률 기반 파라미터 자동 튜닝 로직 적용.
        """
        if context is None:
            return signal

        # 승률 계산
        wins = getattr(context, "win_count", 0)
        losses = getattr(context, "loss_count", 0)
        total = wins + losses

        # 데이터가 어느 정도 쌓였을 때만 조정 (최소 5회 거래)
        if total >= 5:
            win_rate = wins / total
            params = policy.get("params", {})

            # Case 1: 승률이 너무 낮음 (< 30%) -> 보수적 진입, 손절 타이트하게
            if win_rate < 0.3:
                # RSI 매수 기준을 낮춰서 더 과매도일 때만 진입
                current_rsi_buy = float(params.get("rsi_buy", 28.0))
                if current_rsi_buy > 20.0:
                    params["rsi_buy"] = current_rsi_buy - 0.05  # 천천히 하향
                
                # 손절(SL)을 조금 더 타이트하게 (예: -3.0 -> -2.9)
                current_sl = float(params.get("sl", -3.0))
                if current_sl < -1.0:
                    params["sl"] = min(-1.0, current_sl * 0.99)

            # Case 2: 승률이 매우 높음 (> 70%) -> 공격적 운용
            elif win_rate > 0.7:
                # 목표 수익(TP) 상향
                current_tp = float(params.get("tp", 1.2))
                if current_tp < 5.0:
                    params["tp"] = current_tp * 1.001

            # Case 3: 승률에 따른 기본 배팅 사이즈 학습 (Learning)
            # 승률이 좋으면 기본 비중을 늘리고, 나쁘면 줄여서 리스크를 관리함
            current_scale = float(params.get("base_size_scale", 1.0))
            if win_rate > 0.6:
                # 승률 60% 이상이면 점진적 증액 (최대 2.0배, 단 엔진에서 자본 한도 체크함)
                params["base_size_scale"] = min(2.0, current_scale * 1.02)
            elif win_rate < 0.4:
                # 승률 40% 미만이면 감액 (최소 0.2배)
                params["base_size_scale"] = max(0.2, current_scale * 0.98)

        # --------------------------------------------------------
        # Profit Trend Tuning (Recent 10 trades)
        # --------------------------------------------------------
        history = getattr(context, "trade_history", [])
        if len(history) >= 5:
            # 최근 10개(또는 그 미만)의 수익률 합계
            recent = list(history)[-10:]
            profit_sum = sum(item[2] for item in recent)  # item[2] is profit_pct

            # 추세가 매우 좋음 (누적 수익률 > 5%) -> 물타기/불타기 간격 좁힘 (공격적)
            if profit_sum > 5.0:
                step_pct = float(params.get("step_pct", 1.0))
                if step_pct > 0.5:
                    params["step_pct"] = step_pct * 0.99

            # 추세가 나쁨 (누적 수익률 < -2%) -> 목표 수익(TP) 낮춰서 빠른 탈출 유도
            elif profit_sum < -2.0:
                tp = float(params.get("tp", 1.0))
                if tp > 0.5:
                    params["tp"] = tp * 0.99

            # 수익 추세가 좋으면 배팅액 추가 부스트
            if profit_sum > 10.0:
                bs = float(params.get("base_size_scale", 1.0))
                params["base_size_scale"] = min(2.5, bs * 1.05)

            # 추세가 매우 나쁨 (누적 수익률 < -5%) -> 쿨다운 (매매 일시 중단)
            if profit_sum < -5.0:
                last_trade_ts = recent[-1][0]
                now = time.time()
                # 마지막 거래 후 1시간 이내라면 쿨다운 적용 (연속 손실 방지)
                if (now - last_trade_ts) < 3600:
                    current_block = float(getattr(context, "entry_block_until_ts", 0.0) or 0.0)
                    if now > current_block:
                        # 30분간 진입 차단
                        context.entry_block_until_ts = now + 1800.0
                        context.entry_block_reason = f"optimizer:bad_trend({profit_sum:.1f}%)"

        # --------------------------------------------------------
        # Enforce Cooldown
        # --------------------------------------------------------
        now = time.time()
        block_until = float(getattr(context, "entry_block_until_ts", 0.0) or 0.0)
        if now < block_until and signal.signal == "buy":
            return StrategySignal("hold")

        return signal
