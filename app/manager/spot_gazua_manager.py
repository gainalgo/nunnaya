# ============================================================
# Upbit FOCUS Manager — 독립 5-State 매니저 (현물 long_only)
# ------------------------------------------------------------
# 가이드 §3.1: StrategyPlugin 금지, 독립 매니저. 자체 tick 루프 +
#   자체 예산 + 자체 코인 선정. Bybit FocusManager 와 별도 클래스
#   (INV-2: Bybit FOCUS 불침).
#
# 5-State: IDLE → SELECTING → WATCHING → POSITIONED → COOLDOWN
# 진입 = GreenPen PA/Zone (long_only). 청산 = cycle_tp(TP/SL).
#   ※ 존버(longhold/triage) 결선은 단계4(A·SLArbiter 선행) — 여기선 단순 SL.
# 안전: paper 기본 ON, enabled 기본 OFF. 상태 runtime/upbit/ 영속화.
# ============================================================
from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class FocusState(str, Enum):
    IDLE = "IDLE"
    SELECTING = "SELECTING"
    WATCHING = "WATCHING"
    POSITIONED = "POSITIONED"
    COOLDOWN = "COOLDOWN"


@dataclass
class SpotGazuaConfig:
    enabled: bool = True              # ★ 기본 ON (2026-06-17 부모: "모든 것 ON, paper만 빼고"). paper=True라 실주문 0.
    paper: bool = False               # ★ LIVE (실주문) — 2026-06-21 부모 "3대 전부 Live" (GAZUA 효자방식 실탄 가동). 옛 paper유지 반전. 서버별 paper=True 복귀는 UI/runtime 으로 가능.
    budget: float = 0.0           # 0 = 자동(가용 잔고). 통화 중립 — KRW(Upbit/Bithumb)/USDT(Bybit현물)
    max_positions: int = 3
    max_daily_plans: int = 5
    risk_pct: float = 10.0
    # ── 점수(conviction) 비례 사이징 ── 무조건 1/N 아니라 confidence 점수로 가중.
    #   (Bybit _compute_entry_budget 철학: 강신호=슬롯 가득, 약신호=일부만. per_slot=상한)
    conv_sizing_enabled: bool = True    # OFF=옛 균등 1/N (슬롯 상한 가득)
    conv_size_floor: float = 0.5        # 통과 하한(entry_conf_threshold) 신호가 쓰는 슬롯 비중(0~1). 1=가중 OFF와 동일
    min_conf: float = 0.4
    entry_conf_threshold: float = 0.85
    primary_tf: str = "240"
    top_n: int = 10
    scan_interval_sec: float = 60.0
    # 스캔 제외 마켓 (쉼표 구분) — 거래대금은 통과하나 분석/캔들에서 매번 걸려
    #   로그만 더럽히는 코인 운영 제외. ★ 트레이딩 blacklist(손실 감점) 아님 — 요청 자체 차단.
    scan_exclude: str = "KRW-APENFT"
    # ── 거래소 경고 종목 처리 (Entry 탭에서 선택) ── 배지는 항상 표시, 진입 차단만 토글.
    block_warning_coins: bool = True    # 투자유의 종목(상폐위험) 진입 차단(봇+수동). 기본 ON.
    block_caution_coins: bool = False   # 주의환기(가격급등락 등) 진입 차단. 기본 OFF=표시만.
    cooldown_sec: float = 600.0
    tp1_mult: float = 1.2             # 코인용 (가이드 §9.6)
    tp2_mult: float = 2.5
    sl_mult: float = 0.8
    min_rr: float = 1.5
    min_tp_distance_pct: float = 0.3  # fee-guard
    trailing_pct: float = 1.5
    partial_pct: float = 50.0
    stale_hold_hours: float = 0.0     # 0 = 비활성 (시간컷 회의 — 메모리 교훈)
    # ★ TP 방식 — 개미 회전매(% 고정) vs ATR 스윙(변동성 배수)
    #   부모님 철학: 수수료 빼고 자주 작게 익절해 개미처럼 모음 → 가까운 % TP 기본.
    #   ATR 방식(use_pct_tp=False)은 변동성 큰 코인서 TP가 +6~13% 멀어져 안 닿음.
    use_pct_tp: bool = True            # 기본 % 고정 (회전매)
    tp1_pct: float = 1.2              # 진입가 +1.2% → 절반 부분익절
    tp2_pct: float = 2.5             # 진입가 +2.5% → 전량
    sl_pct: float = 1.0              # 진입가 -1.0%
    # ── 현물 청산 결선 (§4.2 · A·SLArbiter 경유 · ★기본 ON, paper로 관측) ──────
    #   SL 도달 시 즉시 매도 대신 "존버(longhold)" — BTC 양호면 매도 보류,
    #   진입가 회복 시 해제. 현물=청산 없음이라 가능(INV-3).
    #   ※ BTC 하락장이면 자동으로 정상 SL 매도(떨어지는 칼 존버 방지, A 내부).
    longhold_enabled: bool = True      # ★ 기본 ON (2026-06-17 부모) — paper로 동작 관측
    longhold_release_pct: float = 0.0  # 0=ATR 동적(ATR%×1.5, clamp 1~8%) / >0=고정 %
    longhold_max_hold_hours: float = 72.0  # 존버 최대 보유(영원 묶임 방지). 0=무제한
    # ── §4 GAZUA 효자방식: DCA(물타기) — SL 전에 평단 낮추며 견딤 → 깊으면 위 존버로 (2026-06-21 부모) ──
    #   원본 = plugin_gazua._common_dca_check (검증값). 현물=청산 없음이라 물타기→존버→회복수확 성립.
    #   초기진입가 기준 step%마다 add_ratio×최초원금 추가(횟수마다 피라미딩), max_depth%까지 = 자동 8단계.
    #   ★ live면 실주문 물타기(지는 포지션 증액) — dca_max_pos_mult(몰빵차단)·dca_abs_sl_pct(진짜 바닥) 방어선 필수.
    dca_enabled: bool = True            # 효자방식 핵심. 0=OFF(단순 SL/존버만)
    dca_step_pct: float = 0.5           # 최초진입가 대비 N%씩 내릴 때마다 1회 추가 (GAZUA 검증값)
    dca_add_ratio: float = 0.25         # 1회 추가 = 최초진입원금 × 이 비율 (피라미딩 전 base)
    dca_max_depth_pct: float = 4.0      # 이 깊이까지만 물타기 (자동 단계수 = depth/step = 8). 이후엔 존버
    dca_pyramid_step: float = 0.20      # 횟수마다 추가배율 +0.20 (완만)
    dca_pyramid_max: float = 2.5        # 추가배율 상한
    dca_max_pos_mult: float = 3.0       # 한 코인 총투입 ≤ 슬롯예산 × 이값 (over-allocation 차단 안전망)
    dca_abs_sl_pct: float = -35.0       # 절대 바닥(최초진입가 기준) — 이하면 존버 무시 강제매도. 0=무제한 존버
    # ★ 떨어지는 칼 게이트(2026-06-23) — DCA가 freefall 중에도 무조건 물타기 → 2배 사이즈로 SL/존버 깨짐
    #   (실측 손실: 수동강제청산 평균 -3041/건, DCA후 빠른 SL -7544@roe -1.9%). 진입엔 momentum_reversal 가드가
    #   있으나 DCA엔 없어 칼받기가 손익비를 역전시킴. → 직전 5M봉이 강하게 하락 중이면 이번 tick 물타기 보류
    #   (바닥 다질 때까지). 효자 눌림목 DCA(멈춘 칼)는 그대로, 떨어지는 칼받기만 차단.
    dca_stabilize_gate_enabled: bool = True   # DCA 전 단기 안정화 확인. 0=OFF(옛 무조건 물타기)
    dca_stabilize_strong_atr: float = 1.0     # 직전 5M봉 하락 ≥ 이값×ATR 이면 칼낙하 판정 → 물타기 보류
    # ── 진입품질 게이트 (§② · 라이브 복귀 게이트 · ★기본 ON, paper로 관측) ─────
    #   부모님 진단: 진입이 천장/끝물 → 컷 더하지 말고 *진입 room*. (feedback_bad_entry_not_fixed_by_cut)
    #   ★ 2026-06-17 부모 "모든 것 ON" — paper라 실주문 0, 매 재시작마다 개선 관측. 0=해당 게이트 OFF.
    headroom_gate_pct: float = 1.0     # 머리 위 저항까지 최소 여유 %. 0=OFF. (천장 추격 차단 게이트)
    atr_sl_floor_mult: float = 0.8     # SL 거리 최소 = mult×ATR (고정%SL 더 좁으면 넓힘). 0=OFF. (잔챙이 1분 노이즈 즉사 방지)
    overext_range_pos_pct: float = 0.85 # 끝물 차단: 24H 범위 상단 이 비율↑이면 차단. 0=OFF. (★ADX 면제 없음)
    overext_min_move_pct: float = 8.0  # 끝물 판정 최소 24H 변동 |%| (작은 변동 제외)
    blowoff_move_pct: float = 30.0     # 파라볼릭 차단: 24H |변동|≥이 %+추격이면 차단. 0=OFF. (overext ADX구멍 보완)
    # ── guard_score (§② G1 · ADX+추세conf 확신점수 · ★기본 ON=표시, 게이트는 threshold>0일 때만) ──
    #   65-80 sweet/80+ 후행 → blind floor 금지. G1=관측(스캐너 score 컬럼), 게이트는 G4(threshold).
    guard_score_mode_enabled: bool = True  # 점수 계산·표시 ON. (threshold=0 이면 진입 차단은 안 함)
    # ★ G4 (2026-06-17): 문턱 게이트 ON. SIDEWAYS 잡설정(≈+25)은 막고 좋은 자리(추세+PA≈65)만 통과.
    #   "업트랜드 없으면 안 들어간다"(부모님 관측) 강제 = 안 들어가는 것도 전략.
    guard_score_threshold: float = 50.0    # 진입 최소 점수. 0=게이트 OFF. >0=미만 차단 (2026-06-17 45→50: 미니백테 54%→고품질, 부모님 소수정예).
    guard_score_total_cap: float = 80.0    # ±cap 클램프 — "80+ 후행" 천장 자름(sweet 상한). 0=무제한.
    # ── ② 청산가드: multi_be_lock (peak 기준 단계별 SL 위로 잠금 = ratchet 이익 보호) ──
    #   peak 이익이 단계를 넘을 때마다 SL 을 위로만 잠금(절대 안 내림). 손실 중엔 미발동.
    #   ★ 컷 아니라 보호 — Bybit 검증(38건 34승). fee-aware(BE락=entry+cushion). 잠금레벨 고정(+0.3/1.0/2.0%).
    multi_be_lock_enabled: bool = True
    multi_be_lock_stage1_pct: float = 0.25   # peak≥0.25% → SL=BE+cushion
    multi_be_lock_stage2_pct: float = 1.0    # peak≥1.0%  → SL=entry+0.3%
    multi_be_lock_stage3_pct: float = 2.0    # peak≥2.0%  → SL=entry+1.0%
    multi_be_lock_stage4_pct: float = 3.0    # peak≥3.0%  → SL=entry+2.0%
    multi_be_lock_fee_cushion_pct: float = 0.1  # Upbit 왕복 수수료(0.05%×2) 쿠션
    # ★ ATR 적응(2026-06-18) — 메이저(저변동 BTC/ETH)는 0.25% peak 가 노이즈 → be_lock 노이즈 발동→BE 컷
    #   (Bybit 현물 0% 승률 회전매 범인). ON 이면 arming floor = max(stage1_pct, ATR%×mult)=노이즈 위에서만 시작.
    multi_be_lock_atr_adaptive: bool = False     # 기본 OFF (paper 관측 후 메이저 거래소서 ON)
    multi_be_lock_atr_mult: float = 2.0          # arming floor = ATR% × 이값 (클수록 더 늦게 잠금 시작)
    # ── ② 청산가드: be_stall intelligent (peak 정체 + 모멘텀 꺾임 → 익절 컷) ──
    #   ★ 컷이라 5중 안전장치(DESIGN_upbit_be_stall_intelligent): 시간윈도우[min,max]·수수료가드·
    #   모멘텀(against 확실할 때만)·중립=보수HOLD·손실/존버 미발동. paper 관측 ON.
    be_stall_enabled: bool = True
    be_stall_sec: float = 60.0                  # peak 정체 최소(이만큼 안 오르면 컷 후보)
    be_stall_max_since_peak_sec: float = 1800.0  # stale 컷오프(이보다 묵은 peak 미발동 — FARTCOIN 사고 방지)
    be_stall_neutral_exit: bool = False         # 중립 모멘텀에서도 시간컷? 기본 False=보수(명확한 역행만 컷)
    be_stall_rsi_strong: float = 55.0           # LONG: RSI≥ = 우리편
    be_stall_rsi_weak: float = 45.0             # LONG: RSI≤ = 반대편
    # ── 수수료 (net PnL · 부모님 직접 입력 2026-06-17) ──────────────────────────
    #   매수+매도 각각 부과 → 왕복 = fee_rate_pct × 2. journal PnL/ROE·미실현 PnL 이 net(수수료 차감).
    #   Upbit KRW 마켓 표준 0.05%/측. 쿠폰/이벤트로 다를 수 있어 부모님이 UI 에서 직접 조정.
    fee_rate_pct: float = 0.05                  # 한쪽(매수 또는 매도) 수수료율 %. 0=수수료 무시(gross).
    # ── paper 슬리피지 (2026-06-24 부모) — paper 가 live 처럼 *불리하게* 체결되도록 모델링 ──
    #   paper 는 client 호출 없이 신호가에 즉시 체결 → 슬리피지 0 = 낙관적(거짓 수익). 이 값만큼
    #   매수=비싸게/매도=싸게 체결로 가정해 paper PnL ≈ live. 편도 bps(5=0.05%). 0=옛 동작(슬립 무시).
    #   얇은 알트 위주면 10~20 이 현실적. LIVE 엔 0 영향(실체결가 그대로).
    paper_slippage_bps: float = 5.0
    # ── 수동 매수(퀵트레이드) 포지션 처리 (2026-06-17 부모) ──────────────────────
    #   퀵트레이드 매수 = self.positions 에 등록 → 패널 표시(슬롯 수 무관·봇 슬롯 미소모).
    #   False(기본·관망): 봇이 안 건드림 — SL/TP 자동 X, 청산 버튼으로 사람이 직접 수확(이윤/손실).
    #   True(봇 관리): 수동 매수도 일반 포지션처럼 SL/TP1/TP2·존버·청산가드로 봇이 자동 관리.
    manual_manage_enabled: bool = False
    # ── CONTRARIAN(역행) 2번째 진입원 (2026-06-18 · plan_contrarian_on_spot · ★기본 OFF) ──────
    #   FOCUS(추세추종) 정반대 regime — 상승추세엔 OFF, 중립/하락(FOCUS churn 장)에서만 진입.
    #   진입만 신설, 실행=_execute_entry 미러(manual=False), 청산=_manage_all_positions 자동 상속
    #   (존버/triage/be_stall = 마스터플랜 §4 현물 청산 모듈, 이미 구현됨). 별도 슬롯·예산.
    #   long_only(현물). conviction-1발 FOCUS 슬롯 불침(별도 슬롯). default OFF=라이브 안전.
    contrarian_enabled: bool = False            # 역행 진입원 ON/OFF (기본 OFF)
    contrarian_max_positions: int = 1           # 역행 별도 슬롯(FOCUS max_positions 와 합산 노출)
    contrarian_coin_up_th: float = 3.0          # 진입 자격: 코인 24h move − BTC move ≥ 이 %(상대강도)
    contrarian_coin_up_cap: float = 15.0        # 파라볼릭 차단: 코인 24h |move| 이 %↑면 제외(펌프 함정). 0=OFF
    contrarian_regime_gate: bool = True         # True=상승추세(BTC UP)엔 진입 안 함(중립/하락만). False=상시
    contrarian_budget: float = 0.0              # 역행 진입 예산. 0=equity의 contrarian_budget_pct% / >0=고정 금액
    contrarian_budget_pct: float = 10.0         # contrarian_budget=0 일 때 equity 대비 비율 %
    contrarian_tp_pct: float = 1.5              # 역행 TP1(부분익절) 진입가 +%
    contrarian_tp2_pct: float = 3.0             # 역행 TP2(전량) 진입가 +%
    contrarian_sl_pct: float = 1.5             # 역행 SL 진입가 -%
    # ── 선물 FOCUS 진입 게이트 *복사* (2026-06-18 · plan_spot_full_chain) ──
    #   선물 본체(focus_manager) 무손상, 검증된 게이트만 spot_entry_guards.py 로 복사-이식.
    #   ★ 2026-06-18 부모 "FOCUS 에 ON 된 것은 현물도 그대로 ON" → 선물 ON 가드 default ON (config_version 8 마이그레이션).
    #     paper=True 라 실주문 0 (관측). momentum_deriv 만 선물도 OFF → OFF 유지.
    #   ★ 2026-06-21 부모: LIVE 전환 후 이 캔들 타이밍 게이트 AND체인이 자동진입을 막아(진입 0, 다 수동) → default OFF 로 풂.
    #     품질/천장방어는 guard_score + headroom/overext/blowoff 로 유지. (paper 관측기엔 ON이 맞았으나 live=재튜닝 필수. 진입 0=고장.)
    #   ① gap_check — 머리 위 N봉 고가까지 거리 < 필요갭 → 차단(천장 바로 밑 진입 금지)
    gap_check_enabled: bool = False              # [2026-06-21 부모] live 자동진입 위해 OFF (캔들 게이트)
    gap_check_tf: str = "60"                     # 갭 측정 TF (5/15/30/60M) — [2026-06-20] 15→60(H1): 3h고가 앵커가 박스장 상시차단(802건) → H1×12=12h 구조저항. 선물 무관(현물 전용)
    gap_check_lookback_bars: int = 12            # 최근 N봉 (60×12=12h 구조 저항)
    gap_check_breakout_exempt: bool = True       # [2026-06-20] 신고가 돌파 시 gap 면제 — 돌파는 천장추격 아님(돌파코인은 늘 자기 고가 코앞이라 앵커만으론 못 풂). 펌프탑은 headroom/끝물/micro가 막음
    gap_check_min_pct: float = 0.3               # 최소 필요 갭 %
    gap_check_atr_adaptive: bool = True          # 등락폭 적응(필요갭 = max(min, ATR%×mult))
    gap_check_atr_mult: float = 0.7
    gap_check_atr_cap_pct: float = 1.5
    #   ② micro_1m — 1M 봉 방향/거래량/RSI "지금 이 순간" 타이밍 (역봉·소진·과열이면 보류)
    micro_1m_check_enabled: bool = False         # [2026-06-21 부모] live 자동진입 위해 OFF (캔들 게이트)
    micro_1m_body_min_pct: float = 0.05          # [2026-06-20] 0.0→0.05: |body|<0.05% 노이즈 도지는 색깔 무관 통과(1m_candle_against 진입0건 교정). 0=종전(색깔만). v9 마이그레이션 적용.
    micro_1m_vol_decline_bars: int = 3           # 거래량 연속 감소 봉수
    micro_1m_rsi_long_max: float = 70.0          # LONG 1M RSI 과열 상한
    micro_1m_rsi_short_min: float = 30.0         # (현물 미사용, 호환 유지)
    #   ③ momentum_reversal — 직전 5M 강한 역행(ATR배) 차단(떨어지는 칼 진입 금지)
    momentum_reversal_enabled: bool = False      # [2026-06-21 부모] live 자동진입 위해 OFF (캔들 게이트)
    momentum_reversal_strong_atr: float = 1.0    # 강한 역행 임계 (×5M ATR)
    momentum_reversal_lookback_bars: int = 3     # 누적 역행 체크 봉수
    #   ④ raw_body — 직전 5M N봉 시가→종가 net 에너지가 진입 반대면 차단 (Phase 2)
    raw_body_enabled: bool = False               # [2026-06-21 부모] live 자동진입 위해 OFF (캔들 게이트)
    raw_body_lookback: int = 3
    raw_body_min_net_pct: float = 0.3            # [2026-06-20] 0.05→0.3: 0.05는 노이즈 드리프트(−0.45% 등)도 다 차단해 전멸(137시도 0진입). 명확한 −0.3%+ 매도만 차단=빈도↑ (선물 복사 미스칼리브 교정, 현물 전용)
    #   ⑤ momentum_deriv — 5M RSI/MACD 변화율이 진입 반대 가속이면 차단 (Phase 2)
    momentum_deriv_enabled: bool = False
    momentum_deriv_lookback: int = 5
    momentum_deriv_rsi_slope: float = 2.0        # RSI 변화 임계
    momentum_deriv_macd_slope: float = 0.0       # MACD hist 변화 임계
    momentum_deriv_require_both: bool = True      # True=RSI+MACD 둘 다 반대일 때만(보수)
    #   ⑥ mtf_align — 상위/단기 TF 구조가 명확히 반대면 차단 (Phase 3, 현물엔 점수만 있었음)
    mtf_align_enabled: bool = False              # [2026-06-21 부모] live 자동진입 위해 OFF (캔들 게이트)
    mtf_align_tfs: str = "240,30,15"             # 검사 TF (D='D' 거래소 지원 시 추가)
    #   ⑦ entry_expectation — reward(도달잠재) 부족/risk(손실폭) 과대 차단 (Phase 3, 공유 유틸 재사용)
    entry_expectation_enabled: bool = True
    entry_expectation_min_reward_pct: float = 0.8   # reward < 이 %면 차단(도달 잠재 부족)
    entry_expectation_max_risk_pct: float = 6.0     # risk > 이 %면 차단(손실폭 과대)
    # ★ [2026-06-20] guard_score 통과 후보는 EE 게이트 면제(선물 focus_manager.py:16685 미러 — not _guard_score_pass).
    #   현물 _scan_and_maybe_enter 의 후보는 전부 guard_score(50) 선택분인데, EE 가 use_pct_tp 고정%(TP+2.5/SL-1.0=실RR2.5)와
    #   분리된 *가짜 zone-RR*(가장 가까운 H1 저항까지)로 재차단하던 것(commit 2e7f404 포팅 시 bypass 누락). room=headroom1%+gap 별도 방어.
    entry_expectation_bypass_guard_score: bool = True  # guard_score 통과분 EE 게이트 면제(★ON·점수통과분 RR게이트 skip·선물 미러. 끄면 옛 동작)
    #   ⑧ microtiming_5m — 5M RSI/MACD/BB 변곡 3종, 2/3 미만이면 이번 tick 보류 (Phase 4, defer=WAIT)
    microtiming_5m_enabled: bool = False         # [2026-06-21 부모] live 자동진입 위해 OFF (캔들 게이트)
    microtiming_5m_min_score: int = 2               # 통과 최소 변곡 점수(0~3)
    microtiming_5m_rsi_long_threshold: float = 35.0  # RSI 과매도 변곡 기준
    microtiming_5m_bb_low_pct: float = 20.0          # BB 하단권 기준 %
    microtiming_5m_bb_recover_pct: float = 30.0      # BB 회복 기준 %
    # ====================================================================
    # BEGIN_FUTURES_672_MIRROR — 선물 FOCUS 코어 672 (선물 FocusConfig verbatim 복사)
    #   2026-06-18 부모: "어설프게 만든 것 버리고 672 설정·기본값으로 다시 셋팅".
    #   ★ 시행착오 결정체 — 값 하나도 안 건드림(손타이핑 0, 선물 소스 직접 추출, 주석까지 그대로).
    #   현재 데이터 봉인만(미배선) → 챕터별로 점수/가드/게이트/청산을 이 필드로 재배선하며
    #   옛 spot-native 근사치 제거 예정. 거래소 단위충돌(turnover USD↔KRW·blacklist 등) 별도 확인.
    # ====================================================================
    rr_ratio: float = 3.0
    max_daily_sl: int = 100        # [2026-04-25 부모님 결정] SL 10회 시 정지 (자본 50% 한도, 더 관대)
    entry_tf: str = "5"            # M5 = "5"
    cycle_tp1_mult: float = 1.8     # Safe 기준: TP1:SL=1.64:1 (기존 0.5=1:1 → 수수료에 잠식)
    cycle_tp2_mult: float = 3.0     # Safe 기준: TP2:SL=2.73:1 (러너 목표)
    cycle_sl_mult: float = 1.1      # [2026-04-25 Long Hold System] SL 멀리 (1.1→2.5, 자본 5% risk per trade)
    partial_exit_pct: float = 50.0
    adx_filter_enabled: bool = True        # ADX 기반 진입 필터
    min_adx_entry: int = 5                # [2026-06-21 부모] 17→5: live 자동진입 위해 ADX 문턱 완화 (SIDEWAYS 차단 풀기)
    # ★ [2026-06-19 부모] ADX 진입게이트 전용 TF — primary_tf(H4=240)는 5일 박스라 변동성 코인이
    #   영영 SIDEWAYS/저ADX. 점수(base conviction)는 이미 6-TF라 단기추세 봄 → 게이트만 짧은 TF로.
    adx_entry_tf: str = "60"              # ADX 진입게이트 TF (H1). "30"/"15" 로 더 짧게 가능. ""=primary_tf 사용
    adx_entry_breakout_exempt: bool = True   # 저ADX여도 직전 닫힌봉이 최근 N봉 고가 돌파면 통과(박스 회복 진입)
    adx_entry_breakout_lookback: int = 12    # 돌파 판정 lookback 봉수 (adx_entry_tf 기준)
    dormant_adx_threshold: int = 15       # DORMANT 기준: 이 이하면 추세 없음
    min_conviction: float = 35.0          # 2026-05-19 Phase 6 Step 1: 50→70 (옛 10점 7점 = 명문대 입학기준, 1823건 분석 결과 옛 conv 7+ 가 큰 이윤 80%)
    phase3_context_bonus_enabled: bool = True  # Phase 3 시간대(±4) + 코인(+2) 가산점 ON/OFF
    scanner_entry: bool = True            # [2026-04-25 default 승격] multi-slot 스캐너 진입 표준화 (False→True)
    scanner_min_adx: int = 25             # [2026-04-18 저녁] 25→18 ($1/min 목표, Anti-Knife+H4가 품질 보장)
    scanner_min_conviction: float = 50.0  # 2026-05-19 Phase 6 Step 1: 30→70 (옛 7점 = 명문대 입학)
    scanner_max_exposure_pct: float = 90.0  # max % of balance used
    scanner_m30_primary_conflict_penalty: float = 1.0   # PRI(H1) vs 30M 추세 충돌 시 conviction 배수 (1.0 = 페널티 없음)
    scanner_m30_direction_conflict_penalty: float = 1.0 # direction(LONG/SHORT) vs 30M 추세 충돌 시 conviction 배수
    entry_mode: str = 'score'                # "score" | "reverse"
    scanner_min_turnover_24h: float = 1_000_000.0   # 유동성 임계 (24h $1M)
    scanner_min_price_usdt: float = 0.10            # 짜잘이 필터 (5.0 → 0.10, APE/CHIP 같은 강신호 진입 가능)
    scanner_top_n: int = 20                         # 추적 코인 수 (10 → 20)
    scanner_blacklist: list = field(default_factory=lambda: ['CLUSDT', 'XAGUSDT', 'XAUTUSDT'])  # 영구 차단 코인 (예: ["CLUSDT"] — Bybit 약관 미동의 등)
    max_same_direction: int = 2            # 같은 방향 최대 포지션 수 (나머지는 반대 방향 강제) — Auto 기본값
    auto_first_dir_lock: bool = False       # [2026-04-26 부모님 결정] Auto 모드 첫 발 방향 잠금
    regime_reversal_pause_enabled: bool = False      # 옛 "전환점=학비 비싼 구간" → 펄스 가드가 대체
    regime_reversal_ema_gap_threshold_pct: float = 0.3  # BTC EMA gap < 0.3% (수렴)
    regime_reversal_adx_threshold: float = 20.0      # ADX < 20 (추세 약화)
    regime_reversal_pause_min: float = 15.0          # 진입 중단 시간 (분)
    conv_sizing_low_threshold: float = 35.0  # [2026-05-17 100점 ×10] 5→50. conv≤50 → budget × 0.5
    conv_sizing_high_threshold: float = 65.0 # [2026-05-17 100점 ×10] 9→90. conv≥90 → budget × 1.5
    conv_risk_scale_enabled: bool = False    # ON/OFF (켜야 발동)
    conv_risk_peak_conv: float = 65.0        # sweet spot 시작(역U 정점) — 진입임계~여기 선형↑
    conv_risk_peak_mult: float = 1.5         # sweet spot risk 배수
    conv_risk_chop_conv: float = 80.0        # 끝물 라인 (이 이상 = 후행/끝물)
    conv_risk_chop_mult: float = 0.6         # 끝물 risk 컷 배수
    conv_risk_floor_mult: float = 0.5        # 진입임계 미만(이례) 안전 배수
    conv_risk_max_mult: float = 2.0          # factor 안전 상한 (폭주 방지)
    btc_trend_conv_bonus_enabled: bool = True
    btc_trend_conv_bonus: float = 20.0       # [2026-05-17 100점 ×10] 2→20
    multi_be_lock_atr_adaptive_enabled: bool = True   # True=ATR 배수 모드 / False=고정 % 모드 (위 stage_pct 사용)
    multi_be_lock_atr_tf: str = "60"                  # ATR 계산 TF (H1)
    multi_be_lock_atr_period: int = 14                # ATR 기간
    multi_be_lock_stage1_atr_mult: float = 0.3        # (적응모드) stage1 trigger = ATR% × 0.3 (BTC~0.3%, HYPE~0.9%)
    multi_be_lock_stage2_atr_mult: float = 0.7        # (적응모드) stage2 trigger = ATR% × 0.7
    multi_be_lock_stage3_atr_mult: float = 1.4        # (적응모드) stage3 trigger = ATR% × 1.4
    multi_be_lock_stage4_atr_mult: float = 2.2        # (적응모드) stage4 trigger = ATR% × 2.2
    multi_be_lock_atr_min_stage1_trigger_pct: float = 0.2  # [2026-06-04 부모] 0.3→0.2 (검증값, stage1 floor 하향)
    multi_be_lock_atr_max_stage1_trigger_pct: float = 3.0
    be_lock_grace_sec: float = 60.0
    be_lock_smart_rsi_check: bool = True       # ① RSI 이윤 방향 → 발동 보류
    be_lock_smart_candle_check: bool = True    # ② 직전 N봉 연속 이윤 방향 → 발동 보류
    be_lock_smart_rsi_long_min: float = 55.0   # LONG: RSI ≥ 이 값 = 달리는 중
    be_lock_smart_candle_count: int = 3        # 직전 N봉 (5M) 연속 이윤 방향
    smart_manual_entry_enabled: bool = True            # ON/OFF (OFF 시 L⏳/S⏳ 버튼 숨김)
    smart_manual_entry_default_timeout_sec: float = 300.0   # 대기 시간 (초). UI는 분 단위 입력 → ×60
    portfolio_sl_rate_enabled: bool = True
    portfolio_sl_rate_window_min: int = 5         # 최근 N분 윈도우
    portfolio_sl_rate_threshold: int = 3          # 윈도우 내 SL hit M건 이상이면 발동
    portfolio_sl_rate_pause_min: int = 15         # 발동 시 신규 진입 차단 시간
    btc_b12_combined_cap_enabled: bool = True
    btc_b12_combined_cap_max: int = 2             # cross_delta + #4_btc_bonus 합산 cap
    parent_roe_guard_enabled: bool = True
    parent_max_roe_loss_pct: float = 31.0          # [2026-05-13 부모님 재조정] 31% — UI 라벨/도움말과 일치. lev 5→SL 6.2% / lev 7→4.43% / lev 10→3.1%. 마진 1/3까지만 잃음 (보수)
    adaptive_cooldown: bool = False         # 연패 횟수별 쿨다운 증가
    emergency_tp_tiers: bool = False       # [2026-04-26 Long Hold default] BTC crash 자동 청산 OFF
    lock_market: str = ''          # 고정 코인 (5-28 부모: 금 전용 첫 행) — 비어있으면 자동 스캔
    dynamic_trailing: bool = True         # [2026-04-26 Long Hold default] OFF (수동 SL은 부모님)
    breakeven_trigger_pct: float = 0.4     # [2026-04-25 default 승격] 빠른 BE 락 (0.4→0.3)
    trailing_preserve_pct: float = 50.0    # [2026-04-18 밤] 50→40 (중대이익 여유 ↑, 먹은 것보다 토하는 문제 해결)
    trailing_small_profit_preserve_pct: float = 40.0  # [2026-04-18 밤] 40→60 (소이익 꽉 잡기)
    trailing_accel_pct: float = 5.0       # [2026-04-18 밤] 5→3 (큰 수익에서 천천히 조이기 → runner 살림)
    trailing_tp_enabled: bool = True
    trailing_tp_min_progress: float = 0.5   # peak/TP1 진행도 이 값 이상일 때 발동
    trailing_tp_follow_low: float = 0.93     # 40~60% 진행 시 follow ratio (peak의 93% 위치에 TP)
    trailing_tp_follow_mid: float = 0.90     # 60~80% 진행 시
    trailing_tp_follow_high: float = 0.87    # 80%+ 진행 시
    be_stall_exit_enabled: bool = True      # [2026-04-26 Long Hold default] OFF (이윤 못내면 못나가)
    be_stall_exit_sec: float = 60.0          # 23s 분기점 + 7s 마진. 코인 변동성: 25(고변동) ~ 35(저변동).
    be_stall_intelligent_enabled: bool = True  # [2026-05-15 부모] 모멘텀(MACD/RSI/BB) 연동 지능형 — live 구현
    be_stall_intelligent_rsi_strong: float = 55.0    # LONG: RSI >= 55 = 우리편 / SHORT: RSI <= 45
    be_stall_intelligent_rsi_weak: float = 45.0      # LONG: RSI <= 45 = 반대편 / SHORT: RSI >= 55
    min_tp_distance_enabled: bool = True
    microtiming_5m_defer_sec: float = 600.0          # WAIT 후 재평가 간격 (default 10분)
    microtiming_5m_max_defers: int = 3               # 최대 defer 회수 (default 3 → 자연 만료, BLOCK 카운트 안 쌓임)
    microtiming_5m_phase_k_exempt: bool = True       # Phase K (regime transition) 진입은 면제 — 시장 전환 포착 우선
    micro_1m_candle_check: bool = True             # ① 마지막 1M 봉 방향 체크
    micro_1m_candle_trend_exempt_adx: float = 0.0  # ADX 이 이상 = 추세 강함 → 1M 봉 방향 확인 면제 (0=비활성)
    micro_1m_volume_check: bool = True             # ② 1M 거래량 감소 체크
    micro_1m_rsi_check: bool = True                # ③ 1M RSI 극단 체크
    pre_be_stall_exit_mode: str = 'AUTO'                # [2026-04-25 Long Hold System] "AUTO"→"OFF" (BE 도달까지 인내)
    pre_be_stall_min_profit_pct: float = 0.10          # +0.10% 이상에서만 발동 (작은 수익 확정)
    pre_be_stall_sec: float = 240.0                     # [2026-04-25 default 승격] 부모 "91초 너무 이르다" (60→240)
    pre_be_stall_volatility_threshold_pct: float = 2.0 # AUTO 시 횡보/급변동 분기 ATR% (진입 코인 기준)
    pre_be_stall_max_since_peak_sec: float = 1800.0    # peak 후 최대 시간 (default 30분)
    pre_be_loss_guard_enabled: bool = False            # default OFF — 부모 켜야 발동 (paper 관찰 후 실전)
    pre_be_loss_guard_peak_max_pct: float = 0.10       # peak ≤ 이 값 = 헤맴(못 띄움) 대상
    pre_be_loss_guard_trigger_loss_pct: float = 0.5    # entry 대비 -이 값(%) 밀리면 작은 컷 (SL -1~2%의 절반)
    pre_be_loss_guard_min_hold_sec: float = 60.0       # 진입 후 최소 보유 (그레이스 외 추가 안전)
    pre_be_loss_guard_max_age_sec: float = 7200.0      # [2026-06-09 부모] 1800→7200(2h) — 헤맴 -63(30분초과)이 stale룰에 미발동, entry기준이라 묵은peak 사고없이 안전
    overextension_enabled: bool = True                 # 라이브 ON (부모 2026-06-07 "페이퍼 아닌 라이브")
    overextension_range_pos_pct: float = 0.85          # LONG: 24H 범위 상단 이 비율↑ / SHORT: 하단 (1-이값)↓
    overextension_min_move_pct: float = 8.0            # 24H 변동 |%| 이 이상이어야 '끝물' 판정 (작은 변동 제외)
    overextension_penalty: float = 10.0                # conviction 감점 점수
    overextension_adx_exempt: float = 30.0             # ADX 이 이상 = 강한 돌파 → 감점 면제 (0=면제 없음)
    blowoff_filter_enabled: bool = False               # ON/OFF (켜야 발동)
    blowoff_penalty: float = 20.0                      # 기본 감점 (move_pct 지점)
    blowoff_extreme_pct: float = 80.0                  # 24h |변동| ≥ 이 % = 극단 (최대 감점)
    blowoff_max_penalty: float = 40.0                  # 극단에서 최대 감점
    blowoff_chase_only: bool = True                    # True = 같은방향(추격)만 감점 / fade(반대)는 면제
    headroom_penalty_enabled: bool = True              # [2026-06-10 부모 "네가 입력하고 켜라"] 함대 기본 ON
    headroom_sr_penalty: float = 6.0                   # LONG 저항 코앞 / SHORT 지지 코앞 진입 감점
    headroom_sr_near_pct: float = 1.5                  # 저항/지지까지 이 % 이내 = 여력 없음
    headroom_rsi_penalty: float = 6.0                  # LONG 과매수 / SHORT 과매도 진입 감점
    headroom_rsi_overbought: float = 70.0              # LONG: RSI 이 이상 = 갈 곳 없음
    headroom_rsi_oversold: float = 30.0                # SHORT: RSI 이 이하 = 갈 곳 없음
    headroom_bb_penalty: float = 4.0                   # LONG BB 상단 / SHORT BB 하단 진입 감점
    headroom_bb_hi_pctb: float = 0.80                  # %b 이 이상 = 밴드 상단 (LONG 여력 없음)
    headroom_bb_lo_pctb: float = 0.20                  # %b 이 이하 = 밴드 하단 (SHORT 여력 없음)
    inflection_setup_enabled: bool = False             # 켜면 guard 점수에 ㉒ Inflect 항목 추가
    inflection_setup_weight: float = 20.0              # W: 변곡 modifier 최대 크기 스케일
    inflection_setup_cap: float = 20.0                 # 출력 클램프 ±cap
    inflection_setup_base: float = 0.45                # base: 위치만으로 주는 기본 가감(모멘텀 0일 때)
    inflection_setup_slope_scale: float = 0.40         # slope15m tanh 정규화 스케일(%)
    retest_setup_enabled: bool = False                 # 켜면 guard 점수에 ㉓ Retest 항목 추가
    retest_setup_weight: float = 12.0                  # retest 가점 최대 크기(되돌림 품질 ×)
    retest_setup_turn_bonus: float = 4.0               # 되돌림 후 방향대로 turning 시 추가 가점
    retest_retr_lo: float = 0.30                       # 최소 되돌림 비율(이하=천장 추격, 신호 X)
    retest_retr_hi: float = 0.90                       # 이상적 되돌림 상한(+0.3 초과=too-deep 실패)
    retest_pivot_width: int = 2                        # 피벗 high/low 좌우 폭
    retest_fail_pct: float = 0.005                     # 돌파 레벨 이 % 이탈 시 retest 실패
    awaken_sl_enabled: bool = False                    # 각성 SL 적응 ON/OFF (켜야 작동)
    awaken_sl_mode: str = "both"                       # atr / structure / both (SL 거리 기준, 부모님 3모드)
    awaken_atr_ratio: float = 1.3                      # 각성 판정 — 현재 ATR / 과거 ATR 비율
    awaken_atr_lookback: int = 20                      # 과거 ATR 평균 봉수(primary_tf=H4)
    awaken_max_sl_mult: float = 2.5                    # SL 최대 배수 (무한 확장 방지)
    awaken_require_day_align: bool = True              # Day(코인 D1) 순행만 견딤 자격 (역행/미정 제외)
    awaken_swing_lookback: int = 10                    # 구조점(각성의 발) swing 탐색 봉수
    awaken_atr_buffer: float = 0.5                     # 구조점에 ATR 여유 배수
    conviction_ceiling_enabled: bool = False           # default OFF — 부모 켜야 발동 (도구로 적정값 확인 후)
    conviction_ceiling_start: float = 65.0             # 이 이상 conviction = 끝물 후보
    conviction_ceiling_target: float = 50.0            # 끝물을 이 점수로 cap (65 게이트 미달 유도)
    conviction_ceiling_adx_exempt: float = 30.0        # ADX 이 이상 = 벽타기 → 면제 (0=면제 없음)
    guard_score_total_cap_enabled: bool = False        # 가드 가산점 총합 클램프 ON/OFF
    conviction_ceiling_post_guards: bool = False       # True = 끝물 상한을 base+가드 합산 *후* 적용 (가산 부활 차단)
    final_bypass_use_base: bool = False                # True = 점수흡수 bypass 비교를 base conviction 으로 (인플레 무관)
    entry_grace_period_sec: float = 0.0                 # 진입 후 N초 동안 빠른 컷 가드 비활성. 0=OFF, 300=5분 권장.
    market_bias_grace_exit_enabled: bool = False        # default OFF — entry_grace_period_sec 와 함께 켜야 발동
    news_grace_exit_enabled: bool = False               # default OFF
    news_grace_exit_threshold: float = 0.5              # |sentiment| >= 임계면 force exit (1.0=극단)
    long_hold_timeout_enabled: bool = False     # [2026-04-25 Long Hold System] tier1/2=0 일 때 즉시 컷 부작용 → 전체 OFF
    hard_roe_cap_enabled: bool = True           # default OFF (Long Hold 일관성)
    hard_roe_cap_roe_pct: float = -50.0          # 발동 임계 ROE % (default -50, 사실상 거의 안 발동)
    override_slot_enabled: bool = True
    override_min_conviction: float = 55.0        # [2026-05-17 100점 ×10] 8→80. Override Slot 진입 conviction 임계
    override_min_adx: float = 40.0               # ★ 2026-05-11 부모님 A 옵션 (50→40 완화)
    override_min_mtf_align: int = 4              # 모든 TF 일치 (Phase 2B)
    override_min_b12_n: int = 7                  # 8 코인 중 7+ 합의 (Phase 2B)
    override_require_btc_trend_match: bool = True  # BTC trend 일치 필수 (Phase 2B)
    override_max_extra_slots: int = 3            # max +3 (확장 cap)
    override_locked_slot_min_hours: float = 6.0  # ★ window(h) — 지정 시간 이상 묶인 슬롯 만 카운트 (사용자 knob)
    override_size_cap_pct: float = 8.0           # 자본의 8% (일반 20% 의 절반 미만)
    override_max_sl_distance_pct: float = 5.0    # SL 5% (일반 20% 의 1/4)
    override_breakeven_trigger_pct: float = 0.3  # BE 빨리 락 (0.3%)
    override_hard_roe_cut_pct: float = -10.0     # ★ Hard ROE -10% 즉시 컷 (배신 대비)
    momentum_reversal_medium_atr: float = 0.5          # 5m 1봉 역행 ≥ ATR×0.5 → 중간 역행
    momentum_reversal_strong_weight: float = -30.0     # [2026-05-17 100점 ×10] -3→-30. 강한 역행 감점
    momentum_reversal_medium_weight: float = -20.0     # [2026-05-17 100점 ×10] -2→-20. 중간 역행 감점
    long_hold_timeout_tier1_min: float = 5.0           # [2026-05-13] T1 Never-Green: 5분 + peak<0.01% — 진입 직후 한번도 못 녹색이면 컷 (BE Stall의 손실 측 대칭)
    long_hold_timeout_tier1_peak_pct: float = 0.01     # 사실상 "한 번도 안 녹색"
    long_hold_timeout_tier2_min: float = 15.0          # [2026-05-13] T2 "5분 졸업면죄부 차단": 15분 + peak<0.05% — 살짝 녹색이었지만 그 이상 진보 없음
    long_hold_timeout_tier2_peak_pct: float = 0.05
    long_hold_timeout_tier3_min: float = 30.0          # [2026-05-13] T3 BE-distant: 30분 + peak<0.2% — BE 트리거(0.4%) 절반도 못 감 = 동력 없음
    long_hold_timeout_tier3_peak_pct: float = 0.2      # [2026-05-13 신규] tier3에도 peak 조건 추가 (절대시간컷 폐기)
    expectation_progress_exit_enabled: bool = True    # 진행률 기반 청산 (LHT 시간컷 대체)
    expectation_progress_t1_min: float = 240.0          # T1: N분 경과 시
    expectation_progress_t1_pct: float = 30.0          # T1: 목표 진행률 < M% 면 컷
    expectation_progress_t2_min: float = 480.0         # T2: N분 경과 시
    expectation_progress_t2_pct: float = 50.0          # T2: 목표 진행률 < M% 면 컷
    expectation_progress_neg_cut_enabled: bool = True
    expectation_progress_neg_cut_pct: float = -50.0    # 이 이하 진행률 (예: -50 = 목표 반대로 50% 진행)
    expectation_progress_neg_cut_min: float = 30.0     # 이 시간 이상 보유 + 위 조건 충족 시 컷
    entry_expectation_gate_enabled: bool = True       # ★ #1: RR/risk/reward 임계 차단
    entry_expectation_min_rr: float = 1.0              # [2026-05-20 부모 운영] 설계도 1.5 → 부모님 1.0 완화 (진입 장벽 too high)
    breadth_strong_n: int = 8              # STRONG 임계: N/10 코인 일제 = 강한 쓰나미
    breadth_mid_n: int = 6                 # MID 임계: N/10 = 중간 쓰나미
    breadth_aligned_strong: float = 12.0   # 순행(흐름 따름) STRONG 가점 (기회) — 부모님 "적극 역발상"
    breadth_aligned_mid: float = 6.0       # 순행 MID 가점
    breadth_counter_strong: float = -25.0  # 역행(흐름 거스름=떨어지는칼) STRONG 감점 (차단)
    breadth_counter_mid: float = -7.0      # 역행 MID 감점
    regime_counter_strong_cap_enabled: bool = True   # STRONG 역행 시 역행방향 conviction cap ON
    regime_counter_strong_cap: float = 50.0          # cap 값 (scanner/guard 임계 아래 → 진입 X + 점수 확실히 빠짐)
    coin_decouple_enabled: bool = True          # 개별 디커플링 SHORT 해방 ON/OFF (2026-06-12 부모 "기본 ON")
    coin_decouple_long_penalty: float = 12.0    # 디커플링 시 역행 다리(떨어지는칼 잡기) 페널티
    coin_decouple_min_strength: float = 0.5     # 코인 6TF 확신도(0~1) 최소 — 약한 흔들림 제외(럭비공 방지)
    coin_decouple_btc_cache_sec: float = 120.0  # BTC 6TF 방향 캐시 TTL(초) — 스캔당 1회만 계산
    mom_decouple_enabled: bool = False           # 모멘텀 decouple conviction 해방 ON/OFF (켜는 순간 실거래 작동)
    mom_decouple_weight: float = 30.0            # W: conviction 가감 스케일(변곡식). 50점 격차 flip 위해 ~30 (시뮬)
    mom_decouple_cap: float = 35.0              # 출력 클램프 ±cap
    mom_decouple_base: float = 0.45             # base: 위치만의 기본 가감(모멘텀 0). 변곡과 동일
    mom_decouple_up_thr: float = 0.40           # |모멘텀 up| 최소 — 이하면 '꺾임 아님'(노이즈) 미발동
    mom_decouple_div_thr: float = 0.20          # BTC 모멘텀 대비 코인 발산 최소 — 시장 동반 눌림 제외(코인별 격리)
    mom_decouple_pos_hi: float = 0.60           # SHORT 해방 위치 하한(천장) — 바닥 숏 방지
    mom_decouple_pos_lo: float = 0.40           # LONG 해방 위치 상한(바닥) — 천장 롱 방지
    mom_decouple_btc_cache_sec: float = 60.0    # BTC 5m 모멘텀 캐시 TTL(초) — 스캔당 1회만 계산
    macro_compass_enabled: bool = False             # 기본 OFF (paper 검증 후 ON)
    macro_recovering_conv_delta: float = 0.0        # RECOVERING LONG 가점/SHORT 감점 폭 (기본 0=paper 관찰)
    macro_recovering_require_di_adx: bool = True     # 죽은고양이 방어: +DI>-DI flip + ADX≥임계 동반만 LONG 가점
    macro_recovering_min_adx: float = 20.0          # 회복 확인 최소 ADX (추세 살아있음)
    reversal_score: float = 10.0
    d1_trend_weight: float = 1.0   # D1(일봉) 가중 — 큰 그림 방향 (부모님 2026-06-03)
    h4_trend_weight: float = 1.8   # H4(4시간봉) 가중 — UI 에서 조절 (부모님 2026-06-03)
    h1_trend_weight: float = 1.5
    m30_trend_weight: float = 1.2
    m15_trend_weight: float = 1.5
    m5_trend_weight: float = 1.0
    cr_speed_sign_guard_enabled: bool = False
    cr_blowoff_extreme_guard_enabled: bool = False
    cr_blowoff_extreme_ratio: float = 4.0   # speed/ATR 이 값 이상 = 끝물 (낮출수록 더 자주 끝물 판정)
    cr_trend_agree_guard_enabled: bool = False
    cr_trend_agree_lookback: int = 20   # 큰 추세 판정 캔들 수 (5캔들 대비 넓게 — 차트 전체 방향)
    breadth_dir_chg1h_pct: float = 0.3    # 1시간 변화율 임계 % (주력)
    breadth_dir_ema_pct: float = 0.10     # 5분 EMA spread 임계 % (보조)
    entry_flip_require_alignment: bool = True         # ★ #2: FLIP 방향이 H1+30M 둘 다 반대면 차단
    entry_auto_flip_enabled: bool = False              # ★ ICP 사고 후 자동 FLIP 영구 차단
    gap_check_atr_adaptive_enabled: bool = True    # 등락폭 적응 ON (등락장 꼭대기 추격 차단)
    gap_proximity_exit_enabled: bool = False   # 천장/바닥 접근 청산 (default OFF)
    gap_proximity_exit_tf: str = "15"          # 접근 청산 기준 TF: 5 / 15 / 30 / 60
    gap_proximity_exit_pct: float = 0.2        # 접근 임계 % (이 이내 접근 시 청산)
    entry_volatility_gate_enabled: bool = True         # 진입 직전 변동성 도달가능성 검증 (default ON)
    entry_volatility_lookback_tf: str = "5"            # 등락폭 측정 TF (5분봉)
    entry_volatility_lookback_bars: int = 12           # 최근 N봉 (12×5분 = 1시간)
    entry_volatility_min_reach_ratio: float = 0.6      # 최근등락폭/reward거리 ≥ 이 비율이어야 진입 (0.6 = reward의 60% 변동성 필요)
    trend_reversal_enabled: bool = False              # H4 추세 반전 시 자동 청산 (default OFF)
    bb_macd_sw_enabled: bool = False                  # SIDEWAYS BB불리+MACD약화 자동 청산 (default OFF)
    bb_macd_sw_min_hold_hours: float = 2.0            # 발동 최소 보유 시간 (h)
    bb_macd_sw_pnl_low: float = -2.0                  # 발동 pnl 하한 (%)
    bb_macd_sw_pnl_high: float = 0.5                  # 발동 pnl 상한 (%)
    caution_sideways_profit_secure_enabled: bool = False  # 횡보+이윤 자동 익절 (default OFF)
    caution_min_hold_sec: float = 1800.0              # 발동 최소 보유 (초, 30분)
    caution_fee_rate: float = 0.00055                 # 수수료율 (taker per side)
    caution_min_profit_multiplier: float = 3.0        # 최소 순이익 = 수수료 × N배
    quick_tp_enabled: bool = False                    # 시간 기반 빠른 TP (default OFF, Long Hold 충돌)
    quick_tp_min_hold_hours: float = 8.0              # 발동 최소 보유 (h)
    quick_tp_min_pnl_pct: float = 1.0                 # 발동 최소 pnl (%)
    btc_crash_threshold_pct: float = -5.0             # BTC 급락 자동 청산 임계 (%, emergency_tp_tiers 필요, default OFF)
    btc_emergency_pause_enabled: bool = True          # BTC 급변동 감지 ON/OFF
    btc_emergency_pause_threshold_pct: float = 2.0    # [2026-04-26] default 2% — trader 직관 자주 발동
    btc_emergency_pause_window_min: float = 5.0       # [2026-04-26] default 5분 — 빠른 반응
    btc_emergency_mode: str = "trend_aligned"         # "trend_aligned" / "pause" / "close_all"
    btc_emergency_aggressive_entry: bool = True       # 빈 슬롯 트렌드 방향 진입 가속 ON/OFF
    btc_emergency_aligned_duration_min: float = 120.0 # 트렌드 정렬 유지 시간 (분, default 2h)
    min_sl_pct: float = 0.005                         # [2026-04-26 부모 fix] SL 최소 거리 0.5% (이전 0.1% — round 후 entry==sl 즉사 사고)
    max_sl_distance_pct: float = 20.0                 # SL 최대 거리 (%, 99=사실상 비활성)
    max_atr_pct: float = 5.0                          # ATR cap (%, 변동성 큰 코인 보호)
    cycle_min_rr: float = 1.0                         # TP/SL 최소 RR 비율 (1.0=가드 비활성)
    thesis_invalidation_enabled: bool = False   # [2026-04-25 Long Hold System] 구조 변화 컷 OFF (회복 인내)
    thesis_invalidation_min_hold_h: float = 1.0 # 최소 보유 시간 (시간)
    thesis_invalidation_max_peak_pct: float = 0.3  # peak 수익이 이 이하면 "진전 없음"
    sl_dodge_enabled: bool = False            # SL 후퇴 비활성화
    sl_dodge_proximity_pct: float = 1.5       # SL까지 이 %이내 접근 시 발동
    sl_dodge_retreat_pct: float = 1.5         # 후퇴 1회당 가격의 1.5%
    sl_dodge_max_count: int = 3               # 최대 3회 후퇴
    sl_dodge_max_total_pct: float = 5.0       # 원래 SL 대비 최대 총 5% 후퇴
    day_direction_enabled: bool = True
    day_direction_hour_kst: float = 9.0         # 매일 N시 KST 평가 (default 09:00)
    day_direction_btc_adx_min: float = 18.0     # BTC H4 ADX < N → NEUTRAL (추세 미약)
    day_direction_conv_delta: float = 5.0       # 우세 방향 conviction +N (반대 -N) — 0=관찰만
    h4_pa_snapshot_enabled: bool = True
    h4_pa_snapshot_hours_kst: str = "1,5,9,13,17,21"  # CSV — KST hour 4시간 간격 (H4 캔들 종가 시점)
    morning_shield_enabled: bool = True        # 06:00 KST: 수익 포지션 SL 조임
    morning_guard_enabled: bool = False         # 07:00-09:30 KST: 진입 conviction 상향
    morning_shield_lock_pct: float = 50.0      # profit >= 1% 시 보존할 이익 비율 (%)
    morning_guard_conviction_boost: float = 20.0  # [2026-05-17 100점 ×10] 2→20. 아침 conviction threshold 추가분
    morning_guard_end_hour_kst: float = 9.5    # Guard 종료 시각 (9.5 = 09:30 KST)
    event_shield_enabled: bool = True          # 이벤트 윈도우 신규진입 차단 + 보유 SL 조임
    event_shield_times_kst: str = ""           # 이벤트 시각 CSV ("2026-06-10 21:30, 2026-06-11 03:00") — KST
    event_shield_window_min: float = 20.0      # 이벤트 시각 전후 기준 윈도우 (이벤트 後 = 이 값)
    event_shield_lead_min: float = 5.0         # [2026-06-08 부모] 슬리피지 리드 — 이벤트 前은 (window+lead)분 = 군중(±20)보다 먼저 반응
    event_shield_lock_pct: float = 70.0        # [2026-06-08 부모 "더 강하게"] 이익≥1% 시 보존율 / 0.3%↑ 무조건 BE
    event_shield_auto_fetch: bool = True       # [2026-06-08 부모] ForexFactory USD High impact 자동 fetch (수동 입력과 합집합)
    auto_tp_enabled: bool = False              # 트레일링 거두기 ON/OFF (기존 트레일링/TP/SL 과 OR)
    auto_tp_usdt: float = 1.0                  # 무장(arm) 임계 — 순익이 이 값 넘으면 '이 이익 지킨다' 무장 (거두는 선 아님)
    auto_tp_peak_giveback_pct: float = 0.3     # 무장 후 peak 순익에서 이 비율 반납 시 거둠 (0.3 = 30% 반납·부모님 "야무지게")
    auto_sl_pct_enabled: bool = False          # 손실 N% 도달 시 자동컷 (기존 SL과 OR · 평소 OFF 권장)
    auto_sl_pct: float = 2.0                   # 컷 손실률 (%)
    dual_direction_observe: bool = True        # Phase 1 관찰 ON (진입 변경 X · 데이터만 수집)
    gate_ledger_enabled: bool = True           # B: 게이트별 통과/거절 집계 ('왜 침묵했나' 관제판). 관측만·진입 불침·100% 로컬(서버간 Tick 무관). 2026-06-21 부모 관제판 — default ON(paper-observe 원칙, 진입 0 진단 계기판).
    dual_observe_auto_off_weak: bool = False   # C: 약서버(RAM≤임계)에서 observe 자동 OFF (F4 부하↓). observe=record-only라 진입 불변.
    dual_direction_enabled: bool = False       # 양방향 평가로 진입 방향 결정 (OFF=signal 방향 = 기존)
    erosion_guard_enabled: bool = True        # [2026-04-25 Long Hold System] peak 침식 컷 OFF
    erosion_guard_peak_pct: float = 0.5        # 최소 peak 수익률 (%) — 이 이상이었어야 발동
    erosion_guard_ratio: float = 0.3           # 침식 비율 — peak의 30% 이하로 떨어지면 발동
    coin_repeat_brake_enabled: bool = True     # 같은 코인 반복 진입 브레이크
    coin_repeat_free_count: int = 0            # [2026-04-26 Long Hold default] 1번 후 즉시 cooldown
    coin_repeat_cooldown_base: float = 600.0   # 브레이크 기본 단위 (초) — 3분 단위
    coin_repeat_window_hours: float = 24.0     # 카운트 윈도우 (시간)
    sl_decay_enabled: bool = True             # [2026-04-25 Long Hold System] ★ 필수 OFF (Long Hold 정반대)
    sl_decay_2h_ratio: float = 0.7             # 2시간 후 SL 거리를 원래의 70%로
    sl_decay_3h_ratio: float = 0.5             # 3시간 후 SL 거리를 원래의 50%로
    coin_loss_cap_enabled: bool = True         # 24h 누적 손실 초과 시 진입 차단
    coin_loss_cap_amount: float = 200.0        # [2026-04-26 Long Hold default] 부모님 결정 (코인 자격 정신)
    coin_loss_cap_window_hours: float = 24.0   # 롤링 윈도우 (시간)
    per_coin_size_cap_enabled: bool = True
    per_coin_size_cap_pct: float = 30.0        # [2026-05-08 default] 자본의 30%
    post_trade_pause_enabled: bool = True
    post_trade_pause_profit_sec: float = 300.0     # 익절 후 5분 대기
    post_trade_pause_loss_sec: float = 600.0       # 손절 후 10분 대기 (더 긴 반성) — legacy fallback
    post_trade_pause_fastreject_sec: float = 900.0 # fast_reject 후 15분 대기 (타이밍 실패 명확)
    post_trade_pause_loss_sliding_enabled: bool = True   # 기본 ON (부모 결정)
    post_trade_pause_loss_tier1_pct: float = 0.5
    post_trade_pause_loss_tier1_sec: float = 60.0
    post_trade_pause_loss_tier2_pct: float = 2.0
    post_trade_pause_loss_tier2_sec: float = 300.0
    post_trade_pause_loss_tier3_pct: float = 5.0
    post_trade_pause_loss_tier3_sec: float = 1800.0
    post_trade_pause_loss_tier4_pct: float = 10.0
    post_trade_pause_loss_tier4_sec: float = 3600.0
    post_trade_pause_loss_tier5_sec: float = 14400.0     # ≥ tier4_pct (유치장)
    consecutive_loss_pause_enabled: bool = False  # ★ [2026-06-06 부모] 기본 OFF — 시간컷이 회복 LONG 막음(SOL conv96.9 정지 사례). 연패폭주는 SL/max_daily_sl이 담당.
    consecutive_loss_pause_count: int = 5       # N회 연속 손실 (3→5 완화 — 켜도 덜 민감)
    consecutive_loss_pause_min: int = 10        # M분 정지 (30→10 완화 — 켜도 짧게)
    regime_direction_fail_enabled: bool = True
    regime_direction_fail_window_hours: float = 4.0  # 레짐 윈도우 (기본 4H = H4 한 봉)
    regime_direction_fail_max: int = 3               # 허용 실패 횟수 (초과 시 해당 방향 차단)
    drawdown_shield_use_cash_only: bool = True
    drawdown_shield_caution_pct: float = 5.0    # 누적 낙폭 CAUTION 임계 (%)
    drawdown_shield_defend_pct: float = 10.0    # 누적 DEFEND 임계 (%)
    drawdown_shield_crisis_pct: float = 20.0    # 누적 CRISIS 임계 (%)
    drawdown_shield_caution_usd: float = 30.0   # 일간 낙폭 CAUTION 임계 ($)
    drawdown_shield_defend_usd: float = 60.0    # 일간 DEFEND 임계 ($)
    drawdown_shield_crisis_usd: float = 100.0   # 일간 CRISIS 임계 ($)
    drawdown_shield_caution_pen: float = -10.0  # CAUTION conviction penalty (음수)
    drawdown_shield_defend_pen: float = -20.0   # DEFEND penalty
    drawdown_shield_crisis_pen: float = -30.0   # CRISIS penalty
    dm_streak_block_hours: float = 1.0           # 연패 N회 도달 시 차단 시간 (시간), 0=영구
    dm_streak_block_opposite: bool = False       # [2026-04-25 부모 "모지리 방지"] 연패 시 반대 방향도 차단. default OFF (SHORT 전환 기회 유지) / Profit Exit Block 과 대칭.
    direction_exhaustion_enabled: bool = True
    direction_exhaustion_window_sec: float = 900       # 15분 관찰창
    direction_exhaustion_profit_count: int = 2         # 연속 익절 N회면 소진 (2회 권장)
    direction_exhaustion_block_sec: float = 1800       # 30분 해당 방향 하드블록
    profit_exit_block_enabled: bool = True
    profit_exit_block_hours: float = 1.0               # [2026-04-25 default 승격] 기회 재포착 빠르게 (12→1)
    profit_exit_block_min_pnl: float = 0.5             # 이 이하 수익은 노이즈로 제외 (수수료 간신히 넘긴 케이스)
    profit_exit_block_min_consecutive: int = 3         # [2026-04-25 Long Hold System] 연승 4회로 완화 (3→4)
    profit_exit_block_block_opposite: bool = True     # 반대 방향은 기본 허용 (FLIP 기회 보존)
    adx_slope_check_enabled: bool = False   # [2026-04-25 default 승격] 4-21 거래 사망 범인 (True→False) ★
    adx_slope_lookback_bars: int = 3                   # 몇 H4 봉 전 대비 (3봉 = 12시간)
    adx_slope_decline_threshold_pct: float = 2.0       # 3봉 전 ADX 대비 N% 이상 하락 시 skip (노이즈 흡수)
    regime_transition_enabled: bool = False             # ★ 2026-05-11 부모님 결정 — Phase K 활성화 ("뿌리 튼튼하게")
    regime_transition_paper_mode: bool = False         # ★ 2026-05-11 부모님 결정 — 실거래 (paper 검증 완료, 형 88%/동생 94.4%)
    regime_transition_size_mult: float = 0.3           # 1주차 floor 0.3 → 2주차 cap 0.5 (형 Q4 FIXED CAP 권장)
    regime_transition_tp_mult: float = 0.7             # 초단기 TP (regime 굳어지기 전 수확)
    regime_transition_sl_mult: float = 0.8             # 타이트 SL (레짐 오판 시 빠른 컷)
    regime_transition_adx_decline_ratio: float = 0.95  # adx_now < adx_peak_4h * 0.95 (5% 하락)
    regime_transition_ema_gap_threshold_pct: float = 0.3  # BTC |EMA20-EMA50|/price < 0.3%
    regime_transition_min_conviction: float = 55.0     # [2026-05-17 100점 ×10] 8→80. scanner_min_conviction 보다 위
    regime_transition_min_mtf_align: int = 3           # H4/H1/30M 3개 이상 PA 방향 정렬
    regime_transition_last_change_age_min: float = 180.0  # regime 전환 후 3h 경과 필요 (연속 flip 방지)
    regime_transition_daily_fail_limit: int = 3        # 일일 실패 N회 → 24h 자동 OFF
    regime_transition_weekly_fail_limit: int = 5       # 주간 실패 N회 → 1주 OFF (동생 추가)
    s3_gate_enabled: bool = True                       # [2026-04-25 default 승격] Fee-Aware Gate 표준화 (False→True)
    s3_gate_paper_mode: bool = False                   # [2026-04-25 default 승격] live 실차단 (True→False)
    s3_gate_min_net_ev_usdt: float = 0.0               # net_ev <= 이 값이면 차단 (기본 0 = 손익분기)
    s3_gate_fee_multiplier: float = 2.0                # 수수료 × N 안전 마진 (왕복 ×2)
    s3_gate_slippage_bps: float = 5.0                  # 슬리피지 추정 (basis points, 5bp = 0.05%)
    s3_gate_link_multiplier: float = 1.3               # LINK 도박기질 가드 (threshold × 1.3)
    orderbook_depth_sizing_enabled: bool = False       # default OFF — 부모 켜야 발동
    orderbook_depth_max_slippage_pct: float = 0.3      # 이 % 이내 호가까지만 "체결 가능"으로 집계
    orderbook_depth_min_fill_ratio: float = 0.5        # 수용량/의도 < 이 비율이면 진입 skip (너무 얇음)
    fast_reject_v2_enabled: bool = False               # default OFF
    fast_reject_v2_max_sec: float = 30.0               # 진입 후 30초 이내 검사
    fast_reject_v2_peak_threshold_pct: float = 0.05    # peak < 0.05% (사실상 0)
    fast_reject_v2_pnl_pct: float = -0.05              # pnl <= -0.05% 동시 만족
    reentry_cooldown_v2_enabled: bool = True           # ★default ON (2026-06-21 AXS 회전매 fix) — 방금 청산한 같은 코인 N분 재진입 차단. _scan_and_maybe_enter 배선됨.
    reentry_cooldown_v2_min: float = 45.0              # 45분 동일 코인(market) 재진입 차단 (2026-04-23 데이터: 첫 청산 후 30분 안에 96% 추가 손실. 45분 = 96% 방어 + 마진). 다른 코인은 자유.
    pa_double_confirm_enabled: bool = False            # default OFF
    pa_double_confirm_window_sec: float = 60.0         # 60초 내 동일 방향 PA 재확인
    regime_direction_lock_enabled: bool = False
    regime_direction_lock_freeze_sec: float = 3600.0   # regime 변경 후 30분 freeze
    regime_direction_lock_neutral_block: bool = False   # NEUTRAL이면 양방향 차단 (REST)
    regime_lock_use_slope: bool = False                 # EMA20 기울기 체크 (꺼면 slope 통과)
    regime_lock_use_distance: bool = False             # [2026-04-25 default 승격] distance 완화 (True→False)
    regime_lock_use_cross: bool = False                 # EMA20 vs EMA50 교차 체크 (꺼면 cross 통과 — 코어 완화)
    imminent_flip_enabled: bool = True
    imminent_flip_ema_gap_pct: float = 0.3            # BTC |EMA20-EMA50|/price*100 임계
    imminent_flip_use_30m: bool = True                # 30M 보조 신호 (False 면 H1 단독)
    imminent_flip_adx_rise_min: float = 2.0           # 최근 lookback 봉 대비 ADX 상승 폭
    imminent_flip_gap_lookback: int = 3               # gap 좁아짐 / ADX 상승 비교 봉 수
    same_coin_flip_cooldown_enabled: bool = True
    same_coin_flip_cooldown_min: int = 60             # 60분 (TAO 47분 후 반대 진입 차단)
    raw_body_guard_enabled: bool = True
    raw_body_guard_lookback: int = 3                  # 최근 3봉 (5m)
    raw_body_guard_min_net_pct: float = 0.0           # >0 으로 net 강도 임계 (0=부호만 본다)
    momentum_deriv_guard_enabled: bool = True
    momentum_deriv_guard_tf: str = "5"                # 5m 봉 (raw_body 와 동일 TF)
    momentum_deriv_guard_lookback: int = 3            # 2026-05-19 동료 통찰 fix: 5→3 (25→15분, 5/18 -3% 같은 급락 잔상 희석). 시간 기반 파생값 본질적 한계 보정.
    momentum_deriv_guard_rsi_min_slope: float = 3.0   # 2026-05-19 동료 통찰 fix: 2.0→3.0 (강한 역방향만 차단, 노이즈 false positive 해소)
    momentum_deriv_guard_macd_min_slope: float = 0.0  # MACD hist 변화량 (0=부호만)
    momentum_deriv_guard_require_both: bool = False    # 2026-05-18 fix: MACD Δ≈0 노이즈 false positive (ENAUSDT 4회 차단) 방지. RSI+MACD 둘 다 반대여야 차단.
    mtf_momentum_align_enabled: bool = True
    mtf_momentum_align_tfs: str = "240,60,30,15,5"    # [2026-05-21 부모] CSV 5단 (H4=240, H1=60, 30M=30, 15M=15, 5M=5) — H1 60%대 깊이 얕음 → 큰그림 H4 + 중간 15M 합의 가중
    mtf_momentum_align_lookback: int = 3              # 각 TF 비교 윈도우
    mtf_momentum_align_min_aligned: int = 1           # 2026-05-19 동료 통찰 fix: 2→1 (H1 강신호 단독 통과, 5/18 급락 잔상이 TF30/TF5 에 남는 자리에서 H1 +14.8 같은 회복 신호 놓치는 문제 해소). 다른 가드 (BB/momentum_deriv/microtiming/본체) 다 살아있어 약한 자리 단독 통과 X.
    mtf_momentum_align_use_macd: bool = True          # MACD 도 포함 (False 면 RSI 만)
    mtf_momentum_align_rsi_slope_thr: float = 0.5     # RSI Δ 부호 판정 임계 (작은 변화 무시)
    cfid_enabled: bool = True
    cfid_tf: str = "60"                               # H1 (변곡점은 H1 기준 가장 안정적)
    cfid_ema_gap_thr_pct: float = 0.4                 # EMA20-50 gap / price * 100 임계
    cfid_volume_spike_ratio: float = 1.5              # 최근 N봉 vol avg / 이전 N봉 vol avg
    cfid_adx_change_min: float = 1.0                  # ADX 변화율 절댓값
    cfid_lookback: int = 5                            # 비교 윈도우
    cfid_bypass_momentum_deriv: bool = True           # momentum_deriv BLOCK 우회 활성
    cfid_bypass_mtf_align: bool = True                # mtf_momentum_align BLOCK 우회 활성
    leading_entry_mode: str = "OFF"                   # "OFF" / "CFID" / "PATTERN"
    cfid_leading_min_strength: float = 70.0           # CFID strength 임계 (1~100)
    cfid_leading_size_pct: float = 5.0                # 진입 사이즈 % of equity (작게)
    cfid_leading_bypass_microtiming: bool = True      # 5m gate 우회
    cfid_leading_bypass_bb_regime: bool = True        # BB_REGIME 정점 차단 우회
    pattern_leading_size_pct: float = 5.0             # 진입 사이즈 % of equity
    pattern_leading_min_5step_score: int = 6          # 5step 12점 만점 중 임계 (S5 retest 포함 권장)
    pattern_leading_max_sr_pct: float = 1.0           # sr_near_S/R 거리 % (지지/저항 가까이)
    pattern_leading_min_mtf_align: int = 2            # mtf_align 정렬 TF 수 (1~4)
    pattern_leading_bypass_microtiming: bool = True
    pattern_leading_bypass_bb_regime: bool = True
    phase6_combo_a_bonus: int = 25                    # 조합 A 가산
    phase6_combo_a_sr_min: int = 5                    # 조합 A: sr_s 최소 (8=near only, 5=mid 까지)
    phase6_combo_a_mtf_min: int = 1                   # 조합 A: mtf_s 최소 (실제 mtf_s 범위 ±2. 2=4 TF 모두 정렬, 1=H1+30M 정렬, 0=정렬 무관)
    phase6_combo_b_bonus: int = 35                    # 조합 B 가산
    phase6_combo_b_strength_min: int = 50             # 조합 B: cfid_strength 최소 (70=강 only, 50=중간)
    phase6_combo_c_bonus: int = 15                    # 조합 C 가산
    phase6_combo_c_5step_min: int = 7                 # 조합 C: 5step score 최소 (10=만점, 7=강자리)
    phase6_combo_d_bonus: int = 15                    # 조합 D 가산
    phase6_combo_d_news_abs_min: int = 6              # 조합 D: |news_raw| 최소 (10=강, 6=중간)
    phase6_combo_e_enabled: bool = True               # E: 직감 가산점 ON/OFF
    phase6_combo_e_bonus_base: int = 50               # E: 기본 가산점 (confidence 80% 이상일 때)
    phase6_combo_e_bonus_max: int = 90                # E: 최대 가산점 (confidence 0% 일 때)
    phase6_combo_e_rsi_overbought: float = 70.0       # E: RSI 과매수 임계 (SHORT 트리거 조건)
    phase6_combo_e_rsi_oversold: float = 30.0         # E: RSI 과매도 임계 (LONG 트리거 조건)
    phase6_combo_e_bb_high_pct: float = 99.0          # E: BB 상단 임계 % (SHORT 트리거 조건)
    phase6_combo_e_bb_low_pct: float = 1.0            # E: BB 하단 임계 % (LONG 트리거 조건)
    phase6_combo_f_enabled: bool = True               # F: 봉 흐름 가산점 ON/OFF
    combo_f_dedupe_enabled: bool = True               # combo_f 방향 이중계산(F1 MTF·F2 M5) 제거 (2026-06-12 부모 "기본 ON")
    phase6_combo_f_mtf_partial_bonus: int = 10        # F1: MTF 부분 정렬 가산
    phase6_combo_f_mtf_full_bonus: int = 20           # F1: MTF 완전 정렬 가산
    phase6_combo_f_m5_dir_bonus: int = 15             # F2: M5 dominant_dir 일치 가산
    phase6_combo_f_m5_body_bonus: int = 15            # F3: M5 avg body 충족 가산
    phase6_combo_f_m5_body_threshold: float = 0.3     # F3: M5 평균 body pct 임계 (강한 봉 흐름)
    charge_exit_enabled: bool = True                  # 점수 회복 자동 청산 ON/OFF
    charge_exit_min_pnl_pct: float = 0.0              # 이윤 조건 (이 이상 pnl% 일 때만 트리거). default 0 = pnl > 0
    charge_exit_conv_delta: float = 5.0               # conv 회복 임계 (baseline + N 증가 시 청산)
    manual_entry_require_combo_f_pass: bool = False    # 수동 진입 combo F 검증 ON/OFF
    manual_entry_combo_f_min: int = 20                 # combo F 최소 점수 (10~50 권장)
    bb_block_threshold_pct: float = 95.0              # LONG > 이값 = hardblock (SHORT < 100-이값 = hardblock 대칭)
    bb_penalty_threshold_pct: float = 85.0            # LONG > 이값 = conv 감점 (SHORT < 100-이값 = 감점 대칭)
    bb_penalty_amount: float = 10.0                   # 감점량 (100점 단위)
    bb_block_trend_bypass_adx: float = 30.0          # ★ [2026-06-06 부모] ① ADX ≥ 이값 = 강한 추세 = BB 벽타기 → BB 극단 차단 우회 (0=비활성). "BB 닿으면 반전" 통념을 추세장에서 해제.
    bb_trend_bypass_require_di: bool = True           # ★ ② 방향 확정 — SHORT면 -DI>+DI (진짜 하락추세)일 때만 벽타기. '튈 것(반전)' 을 거름. DI 못 읽으면 ①만.
    bb_trend_bypass_macd_min: float = 0.0            # ★ ③ MACD 모멘텀 허용치 (0=비활성). >0 이면 진입방향 MACD hist 강도가 이값 이상(가속 중)일 때만 벽타기 — 조여서 더 깐깐하게.
    macro_exit_enabled: bool = False                 # 기본 OFF (청산 가드 — 검증/시뮬 후 ON). RISK_ON+SHORT보유 / RISK_OFF+LONG보유 = 역행
    macro_exit_breadth_min: int = 8                  # 레짐 확실성 — breadth STRONG (N/10 일제) 일 때만 발동 (가짜 화재경보 방어)
    macro_exit_sl_cushion_pct: float = 0.15          # SL 을 현재가에서 이 % 거리로 당김 (가장 가까운 출구; 반등 시 본전, 안 오면 즉시 탈출)
    macro_exit_strong_coin_exempt: bool = True       # 개별 강세 예외 ON (수익 中이면 거시역행이어도 안 자름)
    macro_exit_exempt_min_roe: float = 0.0           # 이 가격ROE% 초과 수익이면 예외 (기본 0=수익이면 무조건 예외)
    coin_state_machine_enabled: bool = True           # 분류 + 로그 + entry dict 박기
    coin_state_apply_conv_adjust: bool = False          # conviction 보정 적용 (default OFF = 검증 후)
    coin_state_accel_conv_adj: float = 0.0              # [2026-05-17 100점 ×10] ACCEL 보정 (default 0)
    coin_state_steady_conv_adj: float = -10.0           # [2026-05-17 100점 ×10] -1→-10. STEADY 보정
    coin_state_decel_conv_adj: float = -20.0            # [2026-05-17 100점 ×10] -2→-20. DECEL 보정
    coin_state_flip_imminent_conv_adj: float = 10.0     # [2026-05-17 100점 ×10] +1→+10. FLIP_IMMINENT 보정
    tight_trail_after_be_enabled: bool = True
    tight_trail_max_slippage_pct: float = 0.2         # peak 에서 N%p 빠지면 컷 (FLOOR = 최소 임계)
    tight_trail_min_peak_pct: float = 0.4             # peak 가 이 값 이상일 때만 적용 (작은 peak 제외)
    tight_trail_atr_adaptive_enabled: bool = True
    tight_trail_atr_tf: str = "5"                     # 5m ATR
    tight_trail_atr_period: int = 14
    tight_trail_atr_multiplier: float = 0.3           # atr_pct × 0.3 = adaptive slippage
    tight_trail_atr_cap_pct: float = 0.6              # 상한 (변동성 매우 큰 자리 보호)
    trend_adaptive_exit_enabled: bool = False         # 켜면 출구 트레일을 코인 ADX에 적응 (실거래 작동)
    trend_adaptive_exit_adx_strong: float = 30.0      # ADX 이상 = runner → 트레일 완화(태움)
    trend_adaptive_exit_adx_weak: float = 18.0        # ADX 이하 = chopper → 트레일 강화(스캘프)
    trend_adaptive_exit_runner_factor: float = 0.6    # runner factor (<1, preserve↓/slip↑ = 더 태움)
    trend_adaptive_exit_chop_factor: float = 1.4      # chopper factor (>1, preserve↑/slip↓ = 빨리 챙김)
    trend_adaptive_exit_adx_cache_sec: float = 30.0   # 코인 ADX 캐시 TTL(초) — 매 tick fetch 방지
    tf_round_tpsl_enabled: bool = True               # 명시 강제 ON (LIVE 포함). 기본은 auto_paper 가 결정
    tf_round_anchor_tf: str = "240"                   # 앵커/거래 TF (H4 = 교재 주력)
    tf_round_atr_period: int = 14
    tf_round_tp_atr_mult: float = 1.0                 # TP1 = ATR × 1.0 (=100% 라운드)
    tf_round_tp2_atr_mult: float = 2.0                # TP2 = ATR × 2.0 (메모리 내부메모 본문 — H4 TP1 $15 / TP2 $30, 부모님 정정 2026-05-28: 4441+30=4471)
    tf_round_sl_ratio: float = 0.333                 # SL 거리 = TP1 × ⅓ (RR 1:3)
    tf_round_anchor_lookback: int = 2                 # kline fetch 여유분
    tf_round_anchor_offset: int = 0                   # 앵커 = PA 결정 직후 forming H4 캔들 (메모리 내부메모 원전, 부모님 정정 2026-05-28: 어제 0→1 fix 폐기)
    tf_round_hold_enabled: bool = True               # 견딤(단기컷 off) — 모드 ON 시 동반
    frame_guard_enabled: bool = True
    frame_guard_range_tf: str = "240"                # 레인지 기준 TF (240=H4, "D"=Daily)
    frame_guard_range_bars: int = 6                  # 최근 N봉 (H4 6봉 = 24h 롤링)
    frame_guard_long_max_pos: float = 0.50           # LONG 허용 최대 위치 (0.5=하단 50%)
    frame_guard_option_b_enabled: bool = True           # 명시 강제 ON (LIVE 포함)
    frame_guard_long_max_pos_b: float = 0.60             # 옵션 B LONG (0.6=하단 60%까지)
    frame_guard_trend_aligned_long_max_pos: float = 0.70    # 강추세 LONG (0.7=하단 70%까지)
    frame_guard_trend_slope_pct: float = 1.5             # n봉 변화율 기준 (24h 1.5%+ = 강추세)
    frame_guard_cooldown_enabled: bool = True
    frame_guard_cooldown_sec: float = 90.0           # 차단 후 같은 (market, direction) silent skip 기간
    h4_pulse_only_enabled: bool = True              # 명시 강제 ON (LIVE 포함)
    h4_pulse_window_min: int = 60                    # H4 마감 후 진입 허용 분 [2026-05-27 30→60 — 다중코인+시간차+테스트 빈도, 노이즈 시 45 검토]
    preclose_entry_enabled: bool = False             # ON/OFF (부모님이 켜야 발동)
    preclose_min_elapsed_pct: float = 88.0           # H4 진행봉 경과율 임계 (88% = 마감 ~29분 전부터)
    preclose_size_ratio: float = 0.5                 # 정규 사이즈 대비 비율 (마감 확인 전 = 절반)
    preclose_wick_ratio_min: float = 1.5             # 핀바 인정: (방향 반대쪽 꼬리)/몸통 ≥ 이 값
    preclose_body_dir_required: bool = True          # 몸통 방향+종가 위치(상/하단 30%) 조건 사용
    preclose_max_per_day: int = 5                    # 일일 선행 진입 상한 (별도 카운터)
    preclose_min_conviction: float = 50.0            # 선행 자격 — base conviction 하한 (소프트 점수 시점=가드前이라 base 기준)
    preclose_topup_enabled: bool = False             # 마감 확인 증액 ON/OFF
    preclose_topup_min_pnl_pct: float = 0.0          # 확인 기준 — H4 마감 후 pnl ≥ N% (default 0 = 손실만 아니면)
    preclose_topup_max_chase_pct: float = 1.0        # 가격이 유리하게 N% 초과 진행했으면 증액 취소(늦은 추격 방지)
    preclose_topup_require_candle_dir: bool = True   # 직전 마감 H4봉이 진입 방향이어야 증액
    preclose_topup_grace_min: float = 60.0           # H4 마감 후 증액 허용 창(분) — 지나면 만료(반 유지)
    anchor_fasttrack_enabled: bool = True           # 명시 강제 ON (LIVE 포함)
    anchor_fasttrack_max_proximity: float = 0.33     # TP1 거리의 ⅓ 안 = fast-track 발동
    pa_completion_enabled: bool = True              # 명시 강제 ON (LIVE 포함)
    pa_completion_huikkang_min_ratio: float = 1.5    # ไส้หลัง body ≥ 직전 평균 × 이 비율
    pa_completion_lookback_bars: int = 3             # ไส้หลัง 직전 N봉 body 평균 계산 (3봉)
    pa_completion_sig_max_ratio: float = 1.0         # Sig body ≤ ไส้หลัง body × 이 비율 (1.0 = ไส้หลัง 보다 작아야)
    guard_score_pa_completion_ok: float = 30.0       # PA Pat 1/2/3 완성 (Sig + ไส้หลัง) ⭐ 부모님 핵심
    guard_score_pa_completion_none: float = -10.0    # PA 패턴 없음 (5-28 부모 완화: -25 → -10, 매번 자동 감점 과함)
    guard_score_d1_pa_ok: float = 25.0               # D1 PA 형성 + 방향 일치 (큰 그림)
    guard_score_d1_pa_none: float = -5.0             # D1 PA 없음 또는 방향 불일치 (5-28 부모 완화: -15 → -5, D1 PA 자체가 드물어)
    guard_score_btc_aligned: float = 15.0            # BTC day_direction 일치 (LONG+LONG / SHORT+SHORT)
    guard_score_btc_opposite: float = -15.0          # BTC 방향 역행
    guard_score_adx_strong: float = 10.0             # ADX ≥ 30 (강추세)
    guard_score_adx_weak: float = -5.0               # ADX < 20 (약추세)
    guard_score_adx_strong_requires_trend: bool = False
    guard_score_vol_big_align: float = 10.0          # 거래량 big + 방향 일치 (5분 평균 대비 2x+)
    guard_score_trend_high_conf: float = 10.0        # H4 trend confidence ≥ 75% (강한 추세)
    guard_score_trend_low_conf: float = -5.0         # H4 trend confidence < 50%
    guard_score_rsi_extreme: float = 10.0            # 5M RSI 극단 + 변곡 (LONG: <30+상승 / SHORT: >70+하락)
    final_30m15m_check_enabled: bool = True          # 30M+15M 둘 다 역행 시 진입 차단
    final_30m15m_bypass_conviction: float = 55.0      # 이 conviction 이상이면 final_30m15m 차단 면제 (0=OFF)
    final_30m15m_bypass_include_regime: bool = False  # True=거시역행도 점수흡수 포함 / False=제외(기존)
    final_d1_bypass_conviction: float = 50.0          # 이 conviction 이상이면 final_d1 차단 면제 (0=OFF, 예 78)
    final_5m_simple_check_enabled: bool = True       # 5M RSI/MACD/BB 진입 방향 동조 검사
    final_5m_simple_min_score: int = 2               # 3종 중 N 이상 동조 시 통과 (max 3)
    final_5m_bb_trend_bypass_enabled: bool = False
    final_d1_alignment_check_enabled: bool = True    # D1 역방향 시 진입 차단
    final_align_regime_override_enabled: bool = True   # 거시 확실 시 final 정렬게이트 거시방향 우선 (default ON)
    final_d1_recent5_override_enabled: bool = False
    final_d1_recent5_drop_pct: float = 1.0   # 최근 5 일봉 변화율 ≤ -이값(%) 이면 UPTREND 라벨 무시 SHORT 통과 (예 1.0)
    d1_reality_demote_enabled: bool = False
    d1_reality_demote_drop_pct: float = 1.0   # 최근 5 일봉 변화율 ≤ -이값(%) 이면 UPTREND→SIDEWAYS 강등 (예 1.0)
    entry_guard_set: str = "green"   # green / yellow / both / minimal
    exit_guard_set: str = "green"    # green / yellow / both / minimal
    exit_5m_emergency_enabled: bool = True          # 명시 강제 ON (LIVE 포함)
    exit_5m_rsi_overbought: float = 70.0             # LONG 청산 RSI 임계 (과매수)
    exit_5m_rsi_oversold: float = 30.0               # SHORT 청산 RSI 임계 (과매도)
    exit_5m_bb_top_pct: float = 90.0                 # LONG 청산 BB position 임계 (상단)
    exit_5m_bb_bottom_pct: float = 10.0              # SHORT 청산 BB position 임계 (하단)
    exit_5m_min_score: int = 2                       # 3종 (RSI/MACD/BB) 중 N 충족 시 청산
    guard_score_h4_pulse_in: float = 20.0            # H4 펄스 창 안 (마감 후 60분)
    guard_score_h4_pulse_out: float = -3.0           # H4 펄스 창 밖 (5-28 부모 완화: -10 → -3, 시간 자리 25% 만족 — 밖이 정상)
    guard_score_h1_pa_in: float = 15.0               # H1 PA 펄스 통과 (창 안 + PA 인식)
    guard_score_h1_pa_out: float = -2.0              # H1 PA 펄스 미통과 (5-28 부모 완화: -5 → -2, H1 PA 자체가 드물어)
    guard_score_frame_aligned: float = 15.0          # Frame Guard 추세 정렬 (강UPTREND+LONG 등)
    guard_score_frame_neutral: float = 5.0           # Frame Guard 중립 자리 (B기본 통과)
    guard_score_frame_opposite: float = -20.0        # Frame Guard 반대편 (고점/바닥잡기)
    regime_align_cap_enabled: bool = False           # 추세정렬(Frame+Trend+AltBTC) 합산 캡 ON/OFF (cap=완화·dedupe=완전제거)
    regime_align_cap: float = 15.0                   # 합산 클램프 한계 (±값). guard_eval 로 SHORT 평균 32→? 관찰 후 조정
    guard_dir_dedupe_enabled: bool = True            # 방향 이중계산(Frame/Trend/AltBTC/BTC정렬) guard 측 제거 (2026-06-12 부모 "기본 ON")
    guard_score_anchor_close: float = 20.0           # Anchor proximity ≤ 0.33 (사이클 시작점)
    guard_score_anchor_far: float = -10.0            # Anchor proximity > 1.0 (라운드 빠짐)
    guard_score_day_box_edge: float = 10.0           # Day Box edge 근처 (반전 자리)
    guard_score_day_box_inside: float = -8.0         # Day Box lock 박스 안 (5-28 부모 완화: -15 → -8)
    guard_score_microtiming_ok: float = 10.0         # microtiming 5M 트리거 충족
    guard_score_microtiming_no: float = -5.0         # microtiming 5M 트리거 X
    guard_score_raw_body_align: float = 5.0          # raw_body 3봉 방향 진입 일치
    guard_score_raw_body_against: float = -8.0       # raw_body 3봉 방향 반대 (5-28 부모 완화: -15 → -8)
    guard_score_momentum_deriv_align: float = 5.0    # momentum_deriv RSI/MACD 일치
    guard_score_momentum_deriv_against: float = -5.0   # momentum_deriv 반대 (5-28 부모 완화: -10 → -5)
    flow_reversal_signal_enabled: bool = False         # paper auto_paper=True, LIVE auto OFF
    flow_reversal_signal_auto_paper: bool = True       # paper 모드 자동 ON
    flow_reversal_bonus_full: float = 30.0             # 5/5 조건 충족 (강한 신호)
    flow_reversal_bonus_strong: float = 20.0           # 4/5 조건
    flow_reversal_bonus_medium: float = 10.0           # 3/5 조건
    flow_reversal_conf_decline_pct: float = 0.25       # Confidence 25%+ 감소 = 약화
    flow_reversal_adx_decline_pct: float = 0.25        # ADX 25%+ 감소 = 약화
    flow_reversal_lookback_samples: int = 6            # 5분 전 비교 (30s scan × 6 = 180s ≈ 3분 lookback 최소)
    alt_btc_alignment_enabled: bool = True             # 항상 평가 (안전)
    alt_btc_aligned_bonus: float = 10.0                # 알트 + BTC 같은 방향 = 인내 가치
    alt_btc_opposite_penalty: float = -10.0            # 알트 - BTC 역방향 = 짧게 빠지기 권장
    day_box_guard_enabled: bool = True              # 명시 강제 ON (LIVE 포함)
    day_box_window_hours: float = 4.0                # 09:00 KST 부터 박스 형성 시간
    day_box_lock_min_hours: float = 3.5              # 이 시점 이후 핑퐁 판정 가능 (불완전 lock 막기)
    day_box_max_atr_ratio: float = 0.8               # 박스 등락폭 / day_h4_atr_pct ≤ 이 값 = 핑퐁 후보
    day_box_min_touches: int = 2                     # 양극점 N회+ 터치 = 핑퐁 확정
    day_box_touch_eps_pct: float = 0.05              # 극점 근접 판정 ε (% 단위, 0.05 = 0.05%)
    day_box_edge_pct: float = 0.05                   # 상하 5% 구간 = "근처" (SHORT 상 95%+/LONG 하 5%-)
    day_box_breakout_pct: float = 0.10               # 박스 돌파 판정 (% 단위, 0.1 = 0.1%)
    h1_pa_pulse_enabled: bool = True                # 명시 강제 ON (LIVE 포함)
    h1_pa_pulse_window_min: int = 15                 # H1 마감 후 진입 허용 분 (H1 ¼ 근사)
    h1_pa_pulse_lookback_bars: int = 2               # 최근 N봉 안 H1 PA 인식 (forming 포함)
    h1_pa_pulse_min_confidence: float = 0.5          # PASignal.confidence 최소 (0.0~1.0)
    h1_pa_pulse_require_day_dir: bool = True         # H4 day_direction 정렬 강제 (NEUTRAL = 통과)
    regime_lock_mode: str = 'OFF'                      # [2026-04-25 default 승격] Scanner Breadth 방식 (B11→B12)
    b12_threshold_n: int = 6                            # N 명 이상 동의해야 방향 결정 (기본 75% of 8)
    b12_window_sec: float = 1200.0                     # 투표 집계 윈도우 (최근 20분 — 2026-04-23 데이터 분석: 평균 사고 19.5분, 중간값 18.3분 매칭)
    coin_reentry_penalty_enabled: bool = True
    coin_reentry_penalty_window_sec: float = 900       # 15분 창
    coin_reentry_penalty_per_count: float = 10.0       # [2026-05-17 100점 ×10] 1→10. 재진입 1회당 conviction -10 누적
    fast_reject_enabled: bool = True           # [2026-04-25 Long Hold System] 5~15분 컷 OFF (회복 인내)
    fast_reject_min_sec: float = 600.0              # 5분 최소 대기 (노이즈 방지)
    fast_reject_max_sec: float = 1500.0              # 15분 이후엔 trend/thesis에 위임
    fast_reject_peak_threshold_pct: float = 0.15    # peak이 이 값 미만이면 "한번도 못 올라옴"
    fast_reject_trigger_pnl_pct: float = -0.5       # 현재 손익이 이 이하여야 발동 (%)
    entry_quality_enabled: bool = True              # 마스터 스위치
    eq_momentum_enabled: bool = True
    eq_momentum_count: int = 2                      # 최근 N봉 검사
    eq_momentum_min_agree: int = 1                  # 최소 K봉 일치
    eq_bb_enabled: bool = True
    eq_bb_upper_pct: float = 80.0                   # LONG 차단: BB% > 이 값 (풀백 매수)
    eq_bb_lower_pct: float = 20.0                   # SHORT 차단: BB% < 이 값 (반등 매도)
    eq_nbar_enabled: bool = True
    eq_nbar_count: int = 5                          # 검사 봉 수
    eq_nbar_min_ratio: float = 0.6                  # HH(or LH) 비율 이상이어야 통과
    manual_exit_penalty_enabled: bool = True
    manual_exit_penalty_hours: float = 0.0          # 손실 탈출 시 쿨다운 (시간)
    session_profile_enabled: bool = True    # [2026-04-25 default 승격] 시간대 ± 표준화 (False→True)
    sess_quiet_start_kst: float = 1.0          # 01:00 KST 시작
    sess_quiet_end_kst: float = 6.0            # 06:00 KST 종료
    sess_quiet_delta: float = -10.0            # [2026-05-17 100점 ×10] -1→-10. quiet 구간 conviction 감점
    sess_active_start_kst: float = 21.0        # 21:00 KST 시작
    sess_active_end_kst: float = 24.0          # 24:00 KST 종료
    sess_active_delta: float = 10.0            # [2026-05-17 100점 ×10] 1→10. active 구간 conviction 가산
    direction_memory_enabled: bool = False  # 옛 "ETH 3연패 방지" → 펄스/Frame Guard 가 대체
    dm_window_count: int = 4                   # 최근 N회 검사
    dm_lookback_days: float = 3.0              # 최대 lookback 일수
    dm_loss_count_penalty: int = 3             # N회 중 K회 손실 시 페널티
    dm_loss_count_delta: float = -5.0         # [2026-05-17 100점 ×10] -2→-20. 페널티 크기
    dm_streak_block_enabled: bool = False      # 옛 "Hard Block 표준화" → 폐기 (PA 펄스 자리 놓침)
    dm_streak_block: int = 2                   # [2026-04-25 Long Hold System] 4연패 → block (3→4, 어차피 존버)
    dm_cache_ttl_sec: float = 180.0            # 3분 캐시 (journal scan 부하 완화)
    btc_regime_enabled: bool = True         # [2026-04-25 default 승격] BTC 역방향 페널티 표준화 (False→True)
    btc_regime_ema_long: int = 50
    btc_regime_trans_band_pct: float = 1.0     # 가격이 EMA50 ±1% 내면 TRANS 후보
    btc_regime_slope_flat_thr_pct: float = 0.3 # EMA20 slope ±이 % 이내면 flat 판정
    btc_regime_cache_ttl_sec: float = 600.0    # 10분 캐시
    btc_regime_bull_long_delta: float = 10.0     # BTC BULL + LONG → 보너스 X (각자도생)
    btc_regime_bear_long_delta: float = -20.0   # BTC BEAR + LONG → 역행 페널티
    btc_regime_trans_delta: float = -10.0         # BTC TRANS → 전환기 불확실 = 페널티 X
    market_bias_enabled: bool = False       # 옛 "쏠림 거스름" → 폐기 (PA 펄스가 진입 방향 결정)
    mb_lookback_trades: int = 12               # 최근 N건
    mb_lookback_hours: float = 6.0             # 최대 시간 범위
    mb_dominance_threshold: float = 0.5        # 쏠림 기준 (0~1)
    mb_min_total: int = 4                      # 최소 sample 수
    mb_against_delta: float = -3.0            # [2026-05-17 100점 ×10] -1→-10. 반대 방향 진입 시 페널티
    mb_cache_ttl_sec: float = 180.0            # 3분 캐시
    pair_block_enabled: bool = True           # ★ 기본 OFF — 활성화 시 반대 방향 차단
    pair_block_mode: str = 'conservative'        # "aggressive" | "conservative"
    pair_block_same_limit: int = 3             # aggressive 모드에서 그룹 내 같은 방향 최대 N개
    coin_profit_lockin_enabled: bool = True   # ★ 기본 OFF
    coin_profit_lockin_window_hours: float = 4.0   # 누적 계산 윈도우
    coin_profit_lockin_min_realized: float = 30.0  # 최소 실현 수익 ($). 이 이하면 Lock-in 적용 안 함
    coin_profit_lockin_protect_ratio: float = 0.7  # 보존선 비율 (70% = 누적 수익의 30%만 반납 허용)
    coin_profit_lockin_require_be: bool = True     # BE 락 이후에만 활성화 (H4 전략 충돌 방지)
    pa_weight_enabled: bool = True             # 기본 ON (부모 결정)
    pa_weight_pin_bar: int = 2                 # PIN_BAR (단기 신호, 노이즈 가능)
    pa_weight_engulfing: int = 3               # ENGULFING (2봉 반전)
    pa_weight_star_v1: int = 5                 # STAR_V1 (3봉 반전, 강력)
    pa_weight_star_v2: int = 5                 # STAR_V2 (3봉 변형, 동급)
    pa_weight_squeeze_break: int = 3           # SQUEEZE_BREAK (변동성 돌파)
    pa_weight_bos: int = 3                     # BOS_BULLISH/BEARISH (구조 돌파)
    pa_weight_zone_bonus: int = 2              # Zone 근접 추가 보너스
    pa_zone_proximity_atr: float = 0.5         # zone 0.5 ATR 이내 = "근처" 인정
    pa_location_penalty_far: float = 0.5       # zone 0.5 ATR 초과 시 PA 점수 × 0.5 (Thai 원본 강조)
    # END_FUTURES_672_MIRROR
    # ── base conviction(spot_conviction.py) 컴포넌트 토글 — 선물엔 있으나 672 코어서 누락된 5개 보강 ──
    #   default True(전부 작동). cfg/UI 로 개별 컴포넌트 끄기용. spot_conviction 가 getattr 로 읽음.
    phase4_rsi_enabled: bool = True
    phase4_mtf_matrix_enabled: bool = True
    phase4_change_rate_enabled: bool = True
    phase4_sr_position_enabled: bool = True
    phase4_volume_pattern_enabled: bool = True
    # config 마이그레이션 버전 — 옛 runtime 의 stale 값이 새 기본을 덮어쓰는 것 방지(_load_state).
    config_version: int = 12                    # v12: gate_ledger 관제판 default ON('왜 침묵했나'·관측만). v11: 캔들 타이밍 게이트 OFF + ADX 완화. v10: paper→False Live 전환. v9: micro_1m_body_min_pct 0.05. v8: 선물 ON 가드 default ON. v7: guard_score 45→50.


@dataclass
class SpotGazuaPosition:
    market: str
    direction: str                    # 항상 "LONG" (현물)
    entry_price: float
    qty: float
    tp1: float
    tp2: float
    sl: float
    atr_used: float
    entry_ts: float
    partial_done: bool = False
    trailing_high: float = 0.0
    krw_spent: float = 0.0
    paper: bool = False
    order_uuid: str = ""
    close_retry_count: int = 0
    tp1_order_uuid: str = ""   # 서버측 지정가 매도(절반) 주문 ID
    tp2_order_uuid: str = ""   # 서버측 지정가 매도(나머지) 주문 ID
    longhold_active: bool = False    # §4.2 존버 전환됨 (SL 매도 보류 중)
    longhold_since_ts: float = 0.0   # 존버 전환 시각 (max_hold cap 기준)
    last_peak_ts: float = 0.0        # trailing_high 마지막 갱신 시각 (be_stall 정체 측정용)
    manual: bool = False             # 퀵트레이드 수동 매수 (관망 모드면 봇 자동관리 제외 — 청산은 사람)
    source: str = "FOCUS"            # 진입원 — "FOCUS"(추세) / "CONTRARIAN"(역행). 슬롯 분리 카운트·UI 배지용
    dca_count: int = 0               # §4 물타기 실행 횟수 (피라미딩·단계 한도 기준)
    dca_initial_entry: float = 0.0   # 최초 진입가 (물타기 깊이·절대바닥 기준 — 평단과 별개로 고정)
    dca_base_krw: float = 0.0        # 최초 진입 원금 (추가 사이즈 base; 평단 낮춰도 불변)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "SpotGazuaPosition":
        return cls(**{k: d[k] for k in d if k in cls.__dataclass_fields__})


class SpotGazuaManager:
    """Upbit 현물 long-only FOCUS — 독립 매니저."""

    _quote_currency = "KRW"   # 견적통화 — 잔고/예산 조회 키. Bybit 현물=USDT 로 override.

    def __init__(self, system: Any = None, client: Any = None, *, state_path: Optional[str] = None):
        self.system = system
        self._lock = threading.RLock()

        if client is None:
            from app.integrations.upbit_trade import UpbitTradeClient
            client = UpbitTradeClient(
                os.getenv("UPBIT_ACCESS_KEY", ""), os.getenv("UPBIT_SECRET_KEY", "")
            )
        self.client = client

        self.config = SpotGazuaConfig()
        # ★ A(SLArbiter) 경유 스위치 (DESIGN_A §A.4). OFF=현 직접 SL 동작 0변화.
        #   env FOCUS_SL_ARBITER_ENABLED 또는 longhold_enabled 둘 중 하나라도 켜지면 A 경유.
        self._sl_arbiter_on = str(os.getenv("FOCUS_SL_ARBITER_ENABLED", "")).strip().lower() in (
            "1", "true", "yes", "on"
        )
        self.state = FocusState.IDLE
        self.positions: List[SpotGazuaPosition] = []
        self.daily_plans_used = 0
        self.daily_sl_count = 0
        self._day_stamp = ""
        self.last_scan_ts = 0.0
        self._last_contra_scan_ts = 0.0   # CONTRARIAN 역행 스캔 타이머 (FOCUS 와 별도)
        self.cooldown_until = 0.0
        self._recent_exit: Dict[str, float] = {}   # market -> 마지막 청산 ts (재진입 쿨다운 v2 기준)
        self._paper_seq = 0
        # ★ [2026-06-21 부모] near-miss 차단 관제 — 점수(guard_score) 통과 후 게이트 차단 기록.
        #   선물 focus_manager._record_near_miss 미러. 현물은 long-only(SHORT 불가)라 '방패'만:
        #   막은 매수가 이후 ↑(아쉬운 차단)=과차단 신호. deque=메모리 최근분, /nearmiss 가 사후판정.
        self._recent_near_miss: deque = deque(maxlen=30)
        self._nm_enrich_box: Dict[str, Any] = {"ts": 0.0, "data": None}   # enrichment 25s 응답캐시(kline 벽 회피)

        if state_path is None:
            try:
                from app.core.runtime_paths import RuntimePaths
                state_path = RuntimePaths(exchange="upbit").custom("upbit_focus_config.json")
            except Exception:
                state_path = os.path.join("runtime", "upbit", "upbit_focus_config.json")
                os.makedirs(os.path.dirname(state_path), exist_ok=True)
        self.state_path = state_path
        # 거래 저널 (JSONL) — Overall Status / Trade Journal 위젯용
        self.journal_path = os.path.join(os.path.dirname(state_path), "upbit_focus_journal.jsonl")
        # ★ [2026-06-20] 저널 전용 락 — write(append)/delete(rewrite)/read 직렬화.
        #   기존엔 락·fsync 없어 청산 동시발생·대시보드 삭제·2프로세스 append 시 파일 깨짐(조각 레코드=기록 누락).
        #   self._lock(무거운 tick RLock) 재사용 시 삭제가 16코인 스캔에 막혀 별도 경량 Lock 사용.
        self._journal_lock = threading.Lock()
        # 계정 요약 TTL 캐시 (status 폴링이 accounts() 를 매번 때리지 않게)
        self._acct_cache: Dict[str, Any] = {}
        self._acct_cache_ts = 0.0

        # ★ [2026-06-21] GateLedger — "오늘 왜 침묵했나"(게이트별 pass/reject) 현물 관제판.
        #   선물 focus_gate_ledger.GateLedger 그대로 재사용(거래소 무관·관측만·진입 1바이트 불침).
        #   ★ 100% 로컬 — 서버간 Tick 안 탐(거래소 Tick 신성불가침). 거래소별 runtime 디렉터리에
        #     영속(spot_gate_stats.json) → 3 현물 인스턴스 한 박스서 충돌 0. 기록은 config.gate_ledger_enabled 일 때만.
        self._gate_ledger = None
        try:
            from app.manager.focus_gate_ledger import GateLedger
            _gl_flush = float(os.getenv("FOCUS_GATE_LEDGER_FLUSH_SEC", "60") or "60")
            _gl_path = os.path.join(os.path.dirname(self.state_path), "spot_gate_stats.json")
            self._gate_ledger = GateLedger(flush_path=_gl_path, flush_sec=_gl_flush)
        except Exception as _gl_exc:
            logger.debug("[SPOT_GAZUA] GateLedger init skipped: %s", _gl_exc)

        self._load_state()
        # ★ scan_exclude 거래소 정합 — 공유 기본값(KRW-APENFT)이 USDT 거래소(Bybit 현물)로 leak → 정리.
        #   이 거래소 견적통화와 안 맞는 마켓(USDT 거래소의 KRW- 마켓 등)은 무해하지만 헷갈려 → 제거.
        if self._sanitize_scan_exclude():
            self._save_state()
        # 마이그레이션 발생 시 새 값 즉시 영속화(다음 재시작부터 v1 — 재발동 방지)
        if getattr(self, "_migrated_v1", False):
            self._save_state()

    @property
    def enabled(self) -> bool:
        return bool(self.config.enabled)

    def _exclude_market_ok(self, mk: str) -> bool:
        """이 거래소 견적통화 마켓 형식인가 (USDT 접미 / KRW- 접두)."""
        m = str(mk).upper()
        return m.endswith("USDT") if self._quote_currency == "USDT" else m.startswith("KRW-")

    def _sanitize_scan_exclude(self) -> bool:
        """scan_exclude 에서 이 거래소와 안 맞는 마켓 제거. 변경 시 True."""
        raw = str(self.config.scan_exclude or "")
        if not raw.strip():
            return False
        kept = [m.strip() for m in raw.split(",") if m.strip() and self._exclude_market_ok(m.strip())]
        new = ",".join(kept)
        if new != raw:
            logger.info("[SPOT_GAZUA] scan_exclude 정합(%s): %r → %r", self._quote_currency, raw, new)
            self.config.scan_exclude = new
            return True
        return False

    # ── 메인 tick ───────────────────────────────────────────
    def tick(self, btc_price: float = 0.0) -> Dict[str, Any]:
        if not self.config.enabled:
            return {"state": "DISABLED"}
        with self._lock:
            self._maybe_reset_daily()
            self._manage_all_positions()

            now = time.time()
            if now < self.cooldown_until and not self.positions:
                self.state = FocusState.COOLDOWN
                return {"state": self.state.value, "positions": 0}

            # ★ 수동(퀵트레이드) 포지션은 봇 슬롯 미소모 — "슬롯 수와 상관없이"(부모 2026-06-17).
            #   봇 자동진입 슬롯은 봇 포지션만 카운트 → 수동 보유가 봇 스캔을 막지 않음.
            #   FOCUS 슬롯 = 봇 진입분 중 수동·CONTRARIAN 제외 (역행은 별도 슬롯).
            bot_positions = sum(1 for p in self.positions
                                if not getattr(p, "manual", False)
                                and getattr(p, "source", "FOCUS") != "CONTRARIAN")
            can_scan = (
                bot_positions < self.config.max_positions
                and self.daily_plans_used < self.config.max_daily_plans
                and now - self.last_scan_ts >= self.config.scan_interval_sec
            )
            if can_scan:
                self.last_scan_ts = now
                self.state = FocusState.SELECTING
                self._scan_and_maybe_enter()

            # ★ CONTRARIAN(역행) 2번째 진입원 — 별도 슬롯/예산/regime-gate. FOCUS 경로 무손상.
            #   conviction-1발 FOCUS 불침(별도 슬롯). 상승추세엔 OFF(중립/하락=FOCUS churn 장만).
            if self.config.contrarian_enabled:
                contra_positions = sum(1 for p in self.positions
                                       if getattr(p, "source", "FOCUS") == "CONTRARIAN")
                if (contra_positions < self.config.contrarian_max_positions
                        and self.daily_plans_used < self.config.max_daily_plans
                        and now - self._last_contra_scan_ts >= self.config.scan_interval_sec):
                    self._last_contra_scan_ts = now
                    self._scan_contrarian_and_maybe_enter()

            return {
                "state": self.state.value,
                "positions": len(self.positions),
                "daily_plans_used": self.daily_plans_used,
            }

    # ── 스캔 + 진입 ─────────────────────────────────────────
    def _ledger_record(self, market: str, gate: str, passed: bool) -> None:
        """게이트 1건 집계(관측만, 진입 흐름 불침). config.gate_ledger_enabled 일 때만 기록.
        예외는 절대 전파 안 함 — 관제판이 진입/청산을 깨면 안 됨."""
        try:
            if self._gate_ledger is not None and getattr(self.config, "gate_ledger_enabled", False):
                self._gate_ledger.record(str(market or "-"), str(gate or "-"), passed=bool(passed))
        except Exception:
            pass

    def _scan_and_maybe_enter(self) -> None:
        # ★ GateLedger 콜백 — ON 일 때만 selector→scanner 가 코인별 게이트 통과/거절을 집계.
        #   OFF 면 None 전달 → 스캔 경로 추가비용 0(동작 0변화).
        _rec = (self._ledger_record
                if (self._gate_ledger is not None and getattr(self.config, "gate_ledger_enabled", False))
                else None)
        try:
            from app.manager.spot_focus_coin_selector import select_spot_focus_coin
            result = select_spot_focus_coin(
                self.system, self.client,
                primary_tf=self.config.primary_tf, top_n=self.config.top_n,
                min_conf=self.config.min_conf, exclude=self.config.scan_exclude,
                headroom_gate_pct=self.config.headroom_gate_pct,
                overext_range_pos_pct=self.config.overext_range_pos_pct,
                overext_min_move_pct=self.config.overext_min_move_pct,
                blowoff_move_pct=self.config.blowoff_move_pct,
                guard_score_mode_enabled=self.config.guard_score_mode_enabled,
                guard_score_threshold=self.config.guard_score_threshold,
                guard_score_total_cap=self.config.guard_score_total_cap,
                block_warning=self.config.block_warning_coins,
                block_caution=self.config.block_caution_coins,
                record=_rec,
            )
        except Exception as exc:
            logger.warning("[SPOT_GAZUA] scan error: %s", exc)
            self.state = FocusState.IDLE
            return

        if not result:
            self.state = FocusState.IDLE
            return

        market = result.get("market", "")
        if not market or any(p.market == market for p in self.positions):
            self.state = FocusState.IDLE
            return

        # ★ 재진입 쿨다운(v2) — 방금 청산한 *같은 코인* 재진입 차단(회전매 방지). default OFF(opt-in).
        #   다른 코인은 자유 → 진입빈도 손상 0. 2026-04-23 데이터: 첫 청산 후 N분 내 재진입이 회전매 핵심.
        if getattr(self.config, "reentry_cooldown_v2_enabled", False):
            _ex_ts = self._recent_exit.get(market, 0.0)
            _cd_sec = float(getattr(self.config, "reentry_cooldown_v2_min", 45.0) or 0.0) * 60.0
            if _ex_ts > 0 and _cd_sec > 0 and (time.time() - _ex_ts) < _cd_sec:
                logger.info("[SPOT_GAZUA] %s 재진입 쿨다운 — 최근 청산 %.0f분 전(<%.0f분), skip",
                            market, (time.time() - _ex_ts) / 60.0, _cd_sec / 60.0)
                self.state = FocusState.IDLE
                return

        conf = float(result.get("confidence", 0) or 0)
        if conf < self.config.entry_conf_threshold:
            try:
                from app.manager.spot_focus_entry_signal import confirm_entry
                ok, reason = confirm_entry(
                    self.client, market, "LONG",
                    conf=conf, threshold=self.config.entry_conf_threshold,
                )
            except Exception as exc:
                ok, reason = False, f"confirm_error:{exc}"
            if not ok:
                logger.info("[SPOT_GAZUA] %s entry held: %s", market, reason)
                self.state = FocusState.WATCHING
                return

        # ★ ADX 진입 게이트 (선물 ADX 상태머신 이식) — 저ADX/SIDEWAYS junk 거부, 다른 게이트보다 먼저.
        #   adx_filter_enabled(기본 True) ON일 때 primary_tf ADX < min_adx_entry → WATCHING 보류.
        #   데이터 부족/에러 = fail-open(통과). 기존 8게이트와 동일 패턴(차단 시 return).
        try:
            from app.manager.spot_guard_chain import adx_entry_gate
            _adx_ok, _adx_why = adx_entry_gate(self.client, market, self.config)
            if not _adx_ok:
                self._ledger_record(market, "ADX(진입게이트)", passed=False)
                self._record_near_miss(market, result.get("conviction_score", result.get("final_score", result.get("guard_score", 0))), "ADX", str(_adx_why), float(result.get("price") or 0))
                logger.info("[SPOT_GAZUA] ⛔ %s ADX 진입 게이트 차단: %s", market, _adx_why)
                self.state = FocusState.WATCHING
                return
        except Exception as _adx_exc:
            logger.debug("[SPOT_GAZUA] adx_entry_gate fail-open: %s", _adx_exc)

        # ★ Phase 1: 선물 진입 타이밍 게이트 복사 (gap/micro_1m/momentum_reversal) — 선택 후보 최종 검증.
        #   전부 default OFF → 켜진 것만 동작(꺼지면 즉시 통과·fetch 0). 차단 시 이번 tick 보류.
        _gate_price = float(result.get("price") or 0) or self._get_price(market)
        _gate_atr = float(result.get("atr") or 0) or self._estimate_atr(market, _gate_price)
        try:
            from app.manager.spot_entry_guards import (
                check_gap, check_micro_1m, check_momentum_reversal, check_raw_body, check_momentum_deriv,
                check_mtf_align, check_entry_expectation, check_microtiming_5m,
            )
            _checks = [
                ("타이밍:gap", check_gap(self.client, market, "LONG", _gate_price, _gate_atr, self.config)),
                ("타이밍:micro_1m", check_micro_1m(self.client, market, "LONG", self.config)),
                ("타이밍:momentum_reversal", check_momentum_reversal(self.client, market, "LONG", self.config)),
                ("타이밍:raw_body", check_raw_body(self.client, market, "LONG", self.config)),
                ("타이밍:momentum_deriv", check_momentum_deriv(self.client, market, "LONG", self.config)),
                ("타이밍:mtf_align", check_mtf_align(self.client, market, "LONG", self.config)),
            ]
            # ★ [2026-06-20] EE 게이트 — guard_score 통과분(이 경로 후보 전부)은 면제(선물 미러). 가짜 zone-RR 재차단 차단.
            #   off 시에만 EE 검사(비-guard_score/fallback 대비 코드 보존). room=headroom+gap·실RR=고정%로 별도 방어.
            if not getattr(self.config, "entry_expectation_bypass_guard_score", True):
                _checks.append(("타이밍:entry_expectation", check_entry_expectation(self.client, market, "LONG", _gate_price, _gate_atr, self.config)))
            _checks.append(("타이밍:microtiming_5m", check_microtiming_5m(self.client, market, "LONG", self.config)))
            for _label, (_ok, _why) in _checks:
                if not _ok:
                    self._ledger_record(market, _label, passed=False)
                    self._record_near_miss(market, result.get("conviction_score", result.get("final_score", result.get("guard_score", 0))), _label, str(_why), _gate_price)
                    logger.info("[SPOT_GAZUA] ⛔ %s 진입 타이밍 게이트 차단: %s", market, _why)
                    self.state = FocusState.WATCHING
                    return
        except Exception as _g_exc:
            logger.debug("[SPOT_GAZUA] entry guards fail-open: %s", _g_exc)

        self._execute_entry(result)

    # ── near-miss 차단 관제 (점수 통과 후 게이트 차단 사후판정 · long-only 방패) ──────
    def _record_near_miss(self, market: str, score: Any, gate: str, reason: str, price: float = 0.0) -> None:
        """점수(guard_score) 통과 후 막판 게이트 차단 = near-miss 기록 (선물 _record_near_miss 미러).
        현물은 SHORT 불가라 '창'(반대방향) 없음 → 순수 '방패' 평가. 막은 매수가 이후 ↑(아쉬운 차단)
        = 과차단 신호(그 게이트 풀 단서). deque(메모리)만 — 기록뿐, 진입/가드 무관. 실패 무해."""
        try:
            _px = float(price or 0.0)
            if _px <= 0:
                try:
                    _px = float(self._get_price(market) or 0.0)
                except Exception:  # noqa: BLE001
                    _px = 0.0
            self._recent_near_miss.append({
                "symbol": (market or "").upper(), "direction": "LONG",
                "score": round(float(score or 0.0), 1),
                "gate": str(gate or "?")[:40], "reason": str(reason or "")[:95],
                "ts": time.time(), "price": _px,
            })
        except Exception:  # noqa: BLE001
            pass

    def get_near_miss_enriched(self) -> List[Dict[str, Any]]:
        """near-miss deque + 차단가 대비 현재/5/15/30/60분 수익률 사후판정 (long-only).
        선물 strategy_focus_router._enrich_near_miss 미러. 25s 응답캐시로 kline 벽 회피(부모 rate-limit 교훈).
        verdict: age<5 관찰중 / ret>+0.10% 아쉬운 차단(과차단) / ret≤+0.05% 좋은 차단 / 그 사이 중립."""
        now = time.time()
        box = getattr(self, "_nm_enrich_box", None)
        if not isinstance(box, dict):
            box = self._nm_enrich_box = {"ts": 0.0, "data": None}
        if box.get("data") is not None and (now - float(box.get("ts") or 0.0)) < 25.0:
            return box["data"]

        _price_cache: Dict[str, float] = {}
        _kline_cache: Dict[tuple, list] = {}

        def _cur(sym: str) -> float:
            if sym in _price_cache:
                return _price_cache[sym]
            try:
                px = float(self._get_price(sym) or 0.0)
            except Exception:  # noqa: BLE001
                px = 0.0
            _price_cache[sym] = px if px > 0 else 0.0
            return _price_cache[sym]

        def _ret(px0: float, px1: float):
            if px0 <= 0 or px1 <= 0:   # long-only: 단순 상승률 (SHORT 부호반전 없음)
                return None
            return round((px1 / px0 - 1.0) * 100.0, 3)

        def _close_at(sym: str, ts0: float, target_ts: float) -> float:
            if ts0 <= 0 or target_ts <= 0 or target_ts > now:
                return 0.0
            age_min = max(0.0, (now - ts0) / 60.0)
            limit = max(24, min(144, int(age_min / 5.0) + 18))
            ck = (sym, limit)
            raw = _kline_cache.get(ck)
            if raw is None:
                try:
                    raw = self.client.get_kline(sym, interval="5", limit=limit) or []
                except Exception:  # noqa: BLE001
                    raw = []
                _kline_cache[ck] = raw
            for row in raw:
                try:
                    ts = float(row[0])
                    if ts > 10_000_000_000:
                        ts = ts / 1000.0
                    close = float(row[4])
                except (IndexError, TypeError, ValueError):
                    continue
                if close > 0 and (ts + 300.0) >= target_ts:
                    return close
            return 0.0

        out: List[Dict[str, Any]] = []
        for n in list(self._recent_near_miss):
            sym = str(n.get("symbol") or "").upper()
            ts0 = float(n.get("ts") or 0.0)
            age_min = round((now - ts0) / 60.0, 1) if ts0 else 0.0
            block_price = float(n.get("price") or 0.0)
            cur = _cur(sym)
            ret_now = _ret(block_price, cur)
            if ret_now is None:
                vkey, vlabel = ("unknown", "판정대기")
            elif age_min < 5.0:
                vkey, vlabel = ("watching", "관찰중")
            elif ret_now > 0.10:
                vkey, vlabel = ("missed_entry", "아쉬운 차단")
            elif ret_now <= 0.05:
                vkey, vlabel = ("good_block", "좋은 차단")
            else:
                vkey, vlabel = ("neutral", "중립")
            rec = {
                "symbol": sym, "direction": "LONG", "score": n.get("score"),
                "reason": n.get("reason"), "gate": n.get("gate") or "?",
                "ts": ts0, "age_min": age_min,
                "block_price": block_price, "price": block_price,
                "current_price": cur, "ret_now_pct": ret_now,
                "verdict": vkey, "verdict_label": vlabel,
            }
            for h in (5, 15, 30, 60):
                if age_min < h or block_price <= 0:
                    rec[f"ret_{h}m_pct"] = None
                else:
                    rec[f"ret_{h}m_pct"] = _ret(block_price, _close_at(sym, ts0, ts0 + h * 60.0))
            out.append(rec)
        out.sort(key=lambda r: r.get("age_min") or 0)
        box["ts"] = now
        box["data"] = out
        return out

    def _compute_targets(self, entry: float, atr: float, *,
                         tp1_pct: Optional[float] = None,
                         tp2_pct: Optional[float] = None,
                         sl_pct: Optional[float] = None):
        """TP1/TP2/SL 계산.
        use_pct_tp=True → 진입가 기준 고정 %(개미 회전매, 부모님 설정값 그대로).
        False → ATR 변동성 배수(cycle_tp, Bybit 스윙). ※기본 % — 숨은 규칙 없음.
        ★ tp1_pct/tp2_pct/sl_pct override 주면(CONTRARIAN) use_pct_tp 무관 % 경로 강제 — 역행 전용 타깃.
          (FOCUS 호출은 override 안 줌 → 동작 100% 불변.)
        """
        from app.strategy.greenpen.cycle_tp import CycleTargets, compute_cycle_targets
        _override = tp1_pct is not None
        if self.config.use_pct_tp or _override:
            _tp1 = tp1_pct if tp1_pct is not None else self.config.tp1_pct
            _tp2 = tp2_pct if tp2_pct is not None else self.config.tp2_pct
            _sl = sl_pct if sl_pct is not None else self.config.sl_pct
            tp1_d = entry * (_tp1 / 100.0)
            sl_d = entry * (_sl / 100.0)
            # ★ §② ATR SL floor — 고정%SL 이 ATR보다 좁으면 ATR로 넓힘(잔챙이 즉사 방지). OFF=그대로.
            from app.manager.spot_entry_quality import atr_floored_sl_distance
            sl_d = atr_floored_sl_distance(entry, sl_d, atr, atr_sl_floor_mult=self.config.atr_sl_floor_mult)
            tp1 = entry + tp1_d
            tp2 = entry * (1 + _tp2 / 100.0)
            sl = entry - sl_d
            return CycleTargets(
                tp1=round(tp1, 8), tp2=round(tp2, 8), sl=round(sl, 8),
                rr_ratio=round(tp1_d / max(sl_d, 1e-12), 2),
                atr_used=atr, direction="LONG",
            )
        return compute_cycle_targets(
            entry, "LONG", atr,
            tp1_mult=self.config.tp1_mult, tp2_mult=self.config.tp2_mult,
            sl_mult=self.config.sl_mult, min_rr=self.config.min_rr,
            min_tp_distance_pct=self.config.min_tp_distance_pct,
        )

    def _execute_entry(self, result: Dict[str, Any]) -> None:
        from app.strategy.greenpen.cycle_tp import compute_position_size

        market = result.get("market", "")
        price = float(result.get("price") or 0) or self._get_price(market)
        if price <= 0:
            logger.warning("[SPOT_GAZUA] no price for %s — skip entry", market)
            self.state = FocusState.IDLE
            return
        atr = float(result.get("atr") or 0) or price * 0.02

        targets = self._compute_targets(price, atr)

        budget = self._effective_budget()                       # 슬롯 상한(총자산÷max_positions)
        # ★ 점수(conviction) 비례 — Phase2 이후에는 base+modifier final score(0~100)를 우선 사용.
        #   구버전/CONTRARIAN fallback 은 GreenPen confidence(0~1) 그대로.
        _score100 = result.get("conviction_score", result.get("final_score", None))
        if _score100 is not None:
            conv01 = max(0.0, min(1.0, float(_score100 or 0) / 100.0))
        else:
            conv01 = float(result.get("confidence", 0) or 0)
        conv_f = self._conv_size_factor(conv01)
        budget *= conv_f                                        # 강신호=상한 가득, 약신호=일부만
        sl_dist = abs(price - targets.sl)
        sizing = compute_position_size(
            budget, self.config.risk_pct, sl_dist, price,
            # 슬롯 분할은 _effective_budget(총자산÷max_positions)에서 이미 처리 →
            # 여기선 무제한(>10)으로 재분할 방지. budget = 이미 '슬롯당 몫 × 점수배율'.
            max_daily_plans=999,
        )
        krw_spend = sizing.qty * price
        min_krw = getattr(self.client, "MIN_ORDER_KRW", 5000.0)
        if krw_spend < min_krw:
            krw_spend = min_krw
        if budget > 0 and krw_spend > budget:
            krw_spend = budget
        if krw_spend < min_krw:
            logger.info("[SPOT_GAZUA] budget %.0f < min order %.0f — skip %s", budget, min_krw, market)
            self.state = FocusState.IDLE
            return
        qty = krw_spend / price

        paper = bool(self.config.paper)
        order_uuid = ""
        if paper:
            # ★ [2026-06-24] paper 슬리피지 — 매수는 불리하게(비싸게) 체결 가정 → 같은 예산에 더 적은 수량.
            #   paper PnL 이 live 에 근접하게(슬리피지 미반영 시 가짜 수익). LIVE 분기엔 무관(실체결가 사용).
            _slip = max(0.0, float(getattr(self.config, "paper_slippage_bps", 0.0))) / 10000.0
            if _slip > 0:
                price *= (1.0 + _slip)
                qty = krw_spend / price
                targets = self._compute_targets(price, atr)
            order_uuid = f"PAPER-{self._paper_seq}"
            self._paper_seq += 1
            logger.info("[SPOT_GAZUA][PAPER] BUY %s krw=%.0f qty=%.8f @ %.2f (sl=%.2f tp1=%.2f tp2=%.2f)",
                        market, krw_spend, qty, price, targets.sl, targets.tp1, targets.tp2)
        else:
            try:
                od = self.client.market_buy(market, krw_spend)
                order_uuid = od.get("uuid", "")
                exec_qty = float(od.get("executed_volume", 0) or 0)
                fill_price = float(od.get("avg_price", 0) or 0)
                # ★ 시장가 매수는 여러 호가에 나눠 체결될 수 있음(예: 18,029 + 452,316원 두 체결).
                #   wait_order 로 전량 체결(state=done)까지 기다린 뒤 *최종 평단/수량* 확정.
                #   부분 체결 평단으로 SL/TP 긋고 나머지 놓치는 것 방지 (부모님 지적).
                if order_uuid:
                    try:
                        od2 = self.client.wait_order(uuid=order_uuid, market=market, timeout_sec=10.0, poll_interval=0.5)
                        exec_qty = float(od2.get("executed_volume", 0) or 0) or exec_qty
                        fill_price = float(od2.get("avg_price", 0) or 0) or fill_price
                    except Exception as q_exc:
                        logger.warning("[SPOT_GAZUA] wait_order reconcile %s 실패: %s", market, q_exc)
                if exec_qty > 0:
                    qty = exec_qty            # 실체결 수량(수수료 반영) — 매도 정합
                if fill_price > 0:
                    price = fill_price        # ★ 실제 체결 평단 → entry_price/TP/SL 기준
                    # 실 평단으로 TP/SL 재계산 (주문 추정가와 슬리피지 차이 보정)
                    targets = self._compute_targets(price, atr)
                logger.info("[SPOT_GAZUA] BUY %s krw=%.0f uuid=%s qty=%.8f @평단 %.4f (sl=%.4f tp1=%.4f tp2=%.4f)",
                            market, krw_spend, order_uuid, qty, price, targets.sl, targets.tp1, targets.tp2)
            except Exception as exc:
                logger.error("[SPOT_GAZUA] BUY FAILED %s: %s", market, exc)
                self.state = FocusState.IDLE
                return

        pos = SpotGazuaPosition(
            market=market, direction="LONG", entry_price=price, qty=qty,
            tp1=targets.tp1, tp2=targets.tp2, sl=targets.sl, atr_used=targets.atr_used,
            entry_ts=time.time(), trailing_high=price, krw_spent=krw_spend,
            paper=paper, order_uuid=order_uuid,
        )
        # ★ live: TP1/TP2 를 거래소 지정가 매도로 미리 박기 (폴링 스파이크 놓침 방지)
        if not paper:
            self._place_tp_orders(pos)
        self.positions.append(pos)
        self.daily_plans_used += 1
        self.state = FocusState.POSITIONED
        self._ledger_record(market, "ENTRY", passed=True)   # 관제판 funnel 꼬리 — 실제 진입 1건
        self._record_journal("ENTRY", pos, price, reason="GreenPen 진입")
        self._save_state()

    # ── CONTRARIAN(역행) 2번째 진입원 ───────────────────────
    #   진입만 신설 — 청산은 _manage_all_positions 가 source 무관 전 포지션 관리(자동 상속).
    def _contrarian_budget(self) -> float:
        """역행 진입 예산. contrarian_budget>0=고정 금액 / 0=equity의 contrarian_budget_pct%.
        실가용 잔고로 cap + 99.5% 버퍼(수수료/슬리피지) — _effective_budget 동일 관례."""
        cfg = self.config
        try:
            if cfg.paper:
                equity = 1_000_000.0
                held = sum(float(p.krw_spent or 0) for p in self.positions)
                free = max(0.0, equity - held)
            else:
                free = float(self.client.get_balance(self._quote_currency))
                held = sum(float(p.krw_spent or 0) for p in self.positions)
                equity = (float(cfg.budget) if cfg.budget > 0 else free + held)
            amt = (float(cfg.contrarian_budget) if cfg.contrarian_budget > 0
                   else equity * (float(cfg.contrarian_budget_pct) / 100.0))
            return max(0.0, min(amt, free) * 0.995)
        except Exception:
            return 0.0

    def _scan_contrarian_and_maybe_enter(self) -> None:
        try:
            from app.manager.spot_focus_coin_selector import select_spot_contrarian_coin
            result = select_spot_contrarian_coin(
                self.system, self.client,
                top_n=self.config.top_n, exclude=self.config.scan_exclude,
                coin_up_th=self.config.contrarian_coin_up_th,
                coin_up_cap=self.config.contrarian_coin_up_cap,
                regime_gate=self.config.contrarian_regime_gate,
                block_warning=self.config.block_warning_coins,
                block_caution=self.config.block_caution_coins,
            )
        except Exception as exc:
            logger.warning("[SPOT_CONTRA] scan error: %s", exc)
            return
        if not result:
            return
        market = result.get("market", "")
        # 같은 마켓 이미 보유(FOCUS/역행/수동 무관) → 중복 진입 금지.
        if not market or any(p.market == market for p in self.positions):
            return
        self._execute_contrarian_entry(result)

    def _execute_contrarian_entry(self, result: Dict[str, Any]) -> None:
        """역행 진입 = _execute_entry 미러. 차이: 역행 예산/타깃 + source="CONTRARIAN".
        manual=False(기본) → _manage_all_positions 가 존버/triage/be_stall 로 자동 관리(상속)."""
        market = result.get("market", "")
        price = float(result.get("price") or 0) or self._get_price(market)
        if price <= 0:
            logger.warning("[SPOT_CONTRA] no price for %s — skip entry", market)
            return
        atr = self._estimate_atr(market, price) or price * 0.02

        def _targets(p):
            return self._compute_targets(
                p, atr, tp1_pct=self.config.contrarian_tp_pct,
                tp2_pct=self.config.contrarian_tp2_pct, sl_pct=self.config.contrarian_sl_pct)
        targets = _targets(price)

        budget = self._contrarian_budget()
        min_krw = getattr(self.client, "MIN_ORDER_KRW", 5000.0)
        krw_spend = budget
        if krw_spend < min_krw:
            logger.info("[SPOT_CONTRA] budget %.0f < min order %.0f — skip %s", budget, min_krw, market)
            return
        qty = krw_spend / price

        paper = bool(self.config.paper)
        order_uuid = ""
        if paper:
            # ★ [2026-06-24] paper 슬리피지 — 매수 불리(비싸게) 체결 가정. LIVE 분기는 무관.
            _slip = max(0.0, float(getattr(self.config, "paper_slippage_bps", 0.0))) / 10000.0
            if _slip > 0:
                price *= (1.0 + _slip)
                qty = krw_spend / price
                targets = _targets(price)
            order_uuid = f"PAPER-{self._paper_seq}"
            self._paper_seq += 1
            logger.info("[SPOT_CONTRA][PAPER] BUY %s krw=%.0f qty=%.8f @ %.2f (sl=%.2f tp1=%.2f tp2=%.2f)",
                        market, krw_spend, qty, price, targets.sl, targets.tp1, targets.tp2)
        else:
            try:
                od = self.client.market_buy(market, krw_spend)
                order_uuid = od.get("uuid", "")
                exec_qty = float(od.get("executed_volume", 0) or 0)
                fill_price = float(od.get("avg_price", 0) or 0)
                if order_uuid:
                    try:
                        od2 = self.client.wait_order(uuid=order_uuid, market=market, timeout_sec=10.0, poll_interval=0.5)
                        exec_qty = float(od2.get("executed_volume", 0) or 0) or exec_qty
                        fill_price = float(od2.get("avg_price", 0) or 0) or fill_price
                    except Exception as q_exc:
                        logger.warning("[SPOT_CONTRA] wait_order reconcile %s 실패: %s", market, q_exc)
                if exec_qty > 0:
                    qty = exec_qty
                if fill_price > 0:
                    price = fill_price
                    targets = _targets(price)   # 실 평단으로 TP/SL 재계산
                logger.info("[SPOT_CONTRA] BUY %s krw=%.0f uuid=%s qty=%.8f @평단 %.4f (sl=%.4f tp1=%.4f tp2=%.4f)",
                            market, krw_spend, order_uuid, qty, price, targets.sl, targets.tp1, targets.tp2)
            except Exception as exc:
                logger.error("[SPOT_CONTRA] BUY FAILED %s: %s", market, exc)
                return

        pos = SpotGazuaPosition(
            market=market, direction="LONG", entry_price=price, qty=qty,
            tp1=targets.tp1, tp2=targets.tp2, sl=targets.sl, atr_used=targets.atr_used,
            entry_ts=time.time(), trailing_high=price, krw_spent=krw_spend,
            paper=paper, order_uuid=order_uuid, source="CONTRARIAN",
        )
        if not paper:
            self._place_tp_orders(pos)
        self.positions.append(pos)
        self.daily_plans_used += 1
        self.state = FocusState.POSITIONED
        self._record_journal("ENTRY", pos, price, reason="CONTRARIAN 역행 진입")
        self._save_state()

    # ── 포지션 관리 (청산) ──────────────────────────────────
    def _manage_all_positions(self) -> None:
        if not self.positions:
            return
        from app.strategy.greenpen.cycle_tp import (
            CycleTargets, should_full_exit, should_partial_exit,
        )

        closed: List[SpotGazuaPosition] = []
        for pos in list(self.positions):
            price = self._get_price(pos.market)
            if not price:
                continue
            if price > pos.trailing_high:
                pos.trailing_high = price
                pos.last_peak_ts = time.time()   # be_stall 정체 측정 기준
            elif pos.last_peak_ts <= 0:
                pos.last_peak_ts = pos.entry_ts  # 로드/구버전 포지션 초기화

            # ★ 관망(watch) 수동 포지션 — 봇 자동관리 전부 skip(SL/TP/be_lock/be_stall/존버).
            #   단 실보유 0(외부/수동 청산) 감지는 유지 → 거래소서 직접 팔면 정리. 청산은 사람(청산 버튼).
            if getattr(pos, "manual", False) and not self.config.manual_manage_enabled:
                if not pos.paper:
                    try:
                        from app.integrations.upbit_trade import base_currency
                        bal = float(self.client.get_balance(base_currency(pos.market), include_locked=True))
                        min_krw = getattr(self.client, "MIN_ORDER_KRW", 5000.0)
                        if bal * price < min_krw:
                            logger.info("[SPOT_GAZUA] %s 관망 수동 포지션 — 외부/수동 청산 감지, 정리", pos.market)
                            self._record_journal("EXIT", pos, price, reason="외부/수동 청산")
                            closed.append(pos)
                    except Exception as exc:
                        logger.debug("[SPOT_GAZUA] %s 관망 잔고 reconcile 실패(무시): %s", pos.market, exc)
                continue

            # ★ 관망→봇관리 전환된 수동 포지션: 타깃 0이면 즉시청산 방지 위해 평단 기준 SL/TP 1회 계산.
            if getattr(pos, "manual", False) and self.config.manual_manage_enabled and pos.sl <= 0:
                _atr = self._estimate_atr(pos.market, pos.entry_price) or pos.entry_price * 0.02
                _t = self._compute_targets(pos.entry_price, _atr)
                pos.sl, pos.tp1, pos.tp2, pos.atr_used = _t.sl, _t.tp1, _t.tp2, _t.atr_used
                if not pos.paper:
                    self._place_tp_orders(pos)
                logger.info("[SPOT_GAZUA] %s 수동 포지션 봇 관리 전환 — SL/TP 계산(sl=%.4f tp1=%.4f)",
                            pos.market, pos.sl, pos.tp1)
                self._save_state()

            # ★ §4.2 존버 코인 회복 → 해제 (정상 관리 복귀). live/paper 공통.
            if pos.longhold_active:
                self._maybe_release_longhold(pos, price)

            # ★ ② multi_be_lock — peak 단계별 SL 위로 잠금(이익 보호). 존버 중엔 미적용.
            if not pos.longhold_active:
                self._apply_multi_be_lock(pos)

            # ★ live: 실보유 잔고 reconcile — 0(또는 dust)이면 외부(수동) 청산으로 간주, 정리.
            #   부모님이 거래소에서 직접 팔면(사람 수확) 봇이 메모리만 믿고 영영 물고 있던 버그.
            if not pos.paper:
                try:
                    from app.integrations.upbit_trade import base_currency
                    # ★ include_locked=True 필수: 서버측 TP 지정가가 코인을 locked 시키므로
                    #   available 만 보면 0 → 외부청산 오판 → 자기 포지션을 죽임(11건 사고).
                    #   수동 청산은 available+locked 둘 다 0 이라 그대로 감지됨.
                    bal = float(self.client.get_balance(base_currency(pos.market), include_locked=True))
                    min_krw = getattr(self.client, "MIN_ORDER_KRW", 5000.0)
                    if bal * price < min_krw:
                        for uid in (pos.tp1_order_uuid, pos.tp2_order_uuid):
                            if uid:
                                try:
                                    self.client.cancel_order(uuid=uid, market=pos.market)
                                except Exception:
                                    pass
                        logger.info("[SPOT_GAZUA] %s 실보유 %.8f(₩%.0f<%.0f) — 외부/수동 청산 감지, 포지션 정리",
                                    pos.market, bal, bal * price, min_krw)
                        self._record_journal("EXIT", pos, price, reason="외부/수동 청산")
                        closed.append(pos)
                        continue
                except Exception as exc:
                    logger.debug("[SPOT_GAZUA] %s 잔고 reconcile 실패(무시): %s", pos.market, exc)
                # ★ 거래소 동기화 — 부모님이 앱에서 TP를 옮기면 봇이 그 가격으로 따라옴
                self._sync_from_exchange(pos, price)

            # ★ ② be_stall intelligent — peak 정체 + 모멘텀 꺾임 → 익절 컷 (live/paper 공통)
            _bs = self._check_be_stall(pos, price)
            if _bs:
                if not pos.paper:   # 서버측 TP 취소 후 시장가
                    for uid in (pos.tp1_order_uuid, pos.tp2_order_uuid):
                        if uid:
                            try:
                                self.client.cancel_order(uuid=uid, market=pos.market)
                            except Exception:
                                pass
                    pos.tp1_order_uuid = ""
                    pos.tp2_order_uuid = ""
                if self._sell_all(pos, _bs):
                    self._record_journal("EXIT", pos, price, reason=_bs)
                    closed.append(pos)
                else:
                    pos.close_retry_count += 1
                    if pos.close_retry_count >= 5:
                        closed.append(pos)
                    self._save_state()
                continue

            # ★ §4 GAZUA 물타기(DCA) — SL 전 평단 낮추며 견딤 (존버/관망 중엔 미발동). live=실주문.
            if self._maybe_dca(pos, price):
                continue   # 평단·타깃 갱신 → 다음 tick 재평가

            # ★ §4 절대 바닥 — 최초진입가 기준 abs_sl 이하 = 존버 무시 강제매도 (무한 물타기/존버 차단)
            if self._dca_abs_floor_breached(pos, price):
                if not pos.paper:
                    for uid in (pos.tp1_order_uuid, pos.tp2_order_uuid):
                        if uid:
                            try:
                                self.client.cancel_order(uuid=uid, market=pos.market)
                            except Exception:
                                pass
                    pos.tp1_order_uuid = ""
                    pos.tp2_order_uuid = ""
                pos.longhold_active = False
                if self._sell_all(pos, "abs_sl"):
                    self.daily_sl_count += 1
                    self._record_journal("EXIT", pos, price,
                                         reason=f"DCA 절대바닥 {self.config.dca_abs_sl_pct:.0f}% 강제매도")
                    closed.append(pos)
                else:
                    pos.close_retry_count += 1
                    if pos.close_retry_count >= 5:
                        closed.append(pos)
                    self._save_state()
                continue

            # ★ live + 서버측 TP 주문 박혀있으면 → 주문 체결 확인 + SL 폴링 (스파이크 면역)
            if not pos.paper and (pos.tp1_order_uuid or pos.tp2_order_uuid):
                if self._manage_live_tp_orders(pos, price):
                    closed.append(pos)
                continue

            targets = CycleTargets(
                tp1=pos.tp1, tp2=pos.tp2, sl=pos.sl,
                rr_ratio=0.0, atr_used=pos.atr_used, direction="LONG",
            )

            reason = should_full_exit(
                price, pos.entry_price, "LONG", targets,
                trailing_high=pos.trailing_high if pos.partial_done else 0.0,
                trailing_pct=self.config.trailing_pct,
            )
            if reason:
                # ★ §4.2: SL 도달은 A 중재(존버 가능). 이익측(TP2/트레일)은 그대로 매도.
                if "SL hit" in reason and not self._resolve_sl_exit(pos, price, reason):
                    continue  # 존버 전환 — 매도 보류
                if self._sell_all(pos, reason):
                    if "SL hit" in reason:
                        self.daily_sl_count += 1
                    self._record_journal("EXIT", pos, price, reason=reason)
                    closed.append(pos)
                else:
                    # 매도 실패 → 포지션 유지·다음 tick 재시도 (orphan 방지).
                    pos.close_retry_count += 1
                    if pos.close_retry_count >= 5:
                        logger.error("[SPOT_GAZUA] %s 매도 5회 실패 — 수동 정리 필요(orphan)", pos.market)
                        closed.append(pos)
                    self._save_state()
                continue

            pe = should_partial_exit(
                price, pos.entry_price, "LONG", targets,
                partial_pct=self.config.partial_pct, already_partial=pos.partial_done,
            )
            if pe:
                sold_q = pos.qty * (pe.exit_pct / 100.0)
                if self._sell_partial(pos, pe.exit_pct):
                    self._book_partial(pos, price, pe.exit_pct / 100.0, sold_qty=sold_q)  # ★저널+원금분할(partial_done set)
                    pos.sl = max(pos.sl, pe.new_sl)  # 본전 보장 (ratchet — be_lock 잠금 안 내림)
                    self._save_state()
                continue

            if self.config.stale_hold_hours > 0:
                hh = (time.time() - pos.entry_ts) / 3600.0
                pnl_pct = (price / pos.entry_price - 1) * 100 if pos.entry_price > 0 else 0
                if hh >= self.config.stale_hold_hours and -1.0 < pnl_pct < 0.5:
                    self._sell_all(pos, f"stale {hh:.0f}h {pnl_pct:.1f}%")
                    self._record_journal("EXIT", pos, price, reason=f"stale {hh:.0f}h")
                    closed.append(pos)

        for pos in closed:
            if pos in self.positions:
                self.positions.remove(pos)
        if closed:
            _now_c = time.time()
            for _cp in closed:
                self._recent_exit[_cp.market] = _now_c   # 재진입 쿨다운(v2) 기준 시각
            self.cooldown_until = _now_c + self.config.cooldown_sec
            if not self.positions:
                self.state = FocusState.COOLDOWN
            self._save_state()

    # ── 수동 강제청산 (사람 수확) ─────────────────────────────────
    def force_close(self, market: str) -> Dict[str, Any]:
        """봇 관리 포지션 1개 전량 매도 후 마감·저널·영속 (paper/live 공통).
        퀵트레이드(/order)는 LIVE 전용·외부 잔고 기준이라 paper 포지션은 못 닫음 —
        이건 봇이 메모리로 들고 있는 포지션을 직접 마감한다. tick 과 같은 lock 사용."""
        with self._lock:
            pos = next((p for p in self.positions if p.market == market), None)
            if pos is None:
                return {"ok": False, "error": f"{market} 포지션 없음"}
            price = self._get_price(market) or pos.entry_price
            if not self._sell_all(pos, "수동 강제청산"):
                return {"ok": False, "error": "매도 실패 — 재시도 필요"}
            self._record_journal("EXIT", pos, price, reason="수동 강제청산")
            if pos in self.positions:
                self.positions.remove(pos)
            self.cooldown_until = time.time() + self.config.cooldown_sec
            if not self.positions:
                self.state = FocusState.COOLDOWN
            self._save_state()
            return {"ok": True, "market": market, "exit": round(price, 8)}

    # ── §4.2 현물 청산 결선 (A·SLArbiter 경유, 존버) ──────────────
    def _spot_freeze_active(self, pos: "SpotGazuaPosition", price: float) -> bool:
        """현물 존버(longhold) 자격 = A 의 freeze_active 입력.
        longhold_enabled + BTC 양호 + 보유 cap 미초과일 때만 True.
        BTC 하락장이면 False → A 가 정상 SL 매도(떨어지는 칼 존버 방지, INV-3)."""
        if not self.config.longhold_enabled:
            return False
        # 존버 최대 보유 cap (영원 묶임 방지) — 초과면 다음 SL 틱에 정상 매도
        cap_h = self.config.longhold_max_hold_hours
        if cap_h > 0:
            since = pos.longhold_since_ts or pos.entry_ts
            if (time.time() - since) / 3600.0 >= cap_h:
                return False
        # BTC 국면 게이트 (strategy_helpers 순수 헬퍼 재사용 — ctx 무관)
        try:
            from app.strategy.strategy_helpers import _check_btc_regime_for_longhold
            return bool(_check_btc_regime_for_longhold())
        except Exception as exc:
            logger.debug("[SPOT_GAZUA] BTC regime 판정 실패 → 존버 미허용: %s", exc)
            return False

    def _longhold_release_pct(self, pos: "SpotGazuaPosition") -> float:
        """존버 해제 임계(%). 0=ATR 동적(ATR%×1.5, clamp 1~8%), >0=고정."""
        if self.config.longhold_release_pct > 0:
            return float(self.config.longhold_release_pct)
        atr = self._estimate_atr(pos.market, pos.entry_price)
        if atr > 0 and pos.entry_price > 0:
            return max(1.0, min(8.0, (atr / pos.entry_price) * 100.0 * 1.5))
        return 2.0

    def _resolve_sl_exit(self, pos: "SpotGazuaPosition", price: float, reason: str) -> bool:
        """SL 도달 처리 — A 에게 물어 매도/존버 결정.
        반환: True=지금 매도 / False=존버(매도 보류).
        A 미경유(스위치 OFF)면 항상 True = 현 직접 SL 동작 0변화."""
        if not (self._sl_arbiter_on or self.config.longhold_enabled):
            return True
        # ★ 이익 보호 SL(be_lock 등, sl≥entry)은 존버 대상 아님 — 익절 확정(떨어지는 칼 아님).
        if pos.entry_price > 0 and pos.sl >= pos.entry_price:
            return True
        from app.manager.sl_arbiter import SLProposal, arbitrate, EXIT_NOW
        freeze = self._spot_freeze_active(pos, price)
        dec = arbitrate(
            has_liquidation=False,  # Upbit 현물 = 청산 없음 (어댑터 선택자와 동일)
            proposals=[SLProposal("cycle_tp", EXIT_NOW, reason=reason)],
            freeze_active=freeze, current_sl=pos.sl,
        )
        if dec.action == EXIT_NOW:
            if pos.longhold_active:  # cap 초과 등으로 존버 해제 후 매도
                pos.longhold_active = False
            return True
        # HOLD = 존버 전환 (매도 보류). 첫 전환 시각·저널 기록.
        if not pos.longhold_active:
            pos.longhold_active = True
            pos.longhold_since_ts = time.time()
            rel = self._longhold_release_pct(pos)
            logger.info("[SPOT_GAZUA] 🔒 존버 전환 %s @%.4f (sl=%.4f, 해제임계 +%.2f%%, BTC 양호)",
                        pos.market, price, pos.sl, rel)
            self._record_journal("LONGHOLD", pos, price, reason=f"SL→존버 (해제 +{rel:.2f}%)")
            self._save_state()
        return False

    # ── §4 GAZUA 효자방식: DCA(물타기) ─────────────────────────────
    def _maybe_dca(self, pos: "SpotGazuaPosition", price: float) -> bool:
        """최초진입가 대비 step%씩 내릴 때마다 1회 추가매수로 평단 낮춤(피라미딩).
        원본 = plugin_gazua._common_dca_check 미러(검증값). 존버/관망 중엔 미발동.
        반환 True = 추가매수 실행됨(평단·타깃 갱신 → 이번 tick 재평가)."""
        cfg = self.config
        if not getattr(cfg, "dca_enabled", False):
            return False
        if pos.manual or pos.longhold_active:
            return False
        if price <= 0 or pos.entry_price <= 0:
            return False
        step = float(getattr(cfg, "dca_step_pct", 0.5) or 0.5)
        if step <= 0:
            return False
        # 최초 진입가/원금 lazy 캡처 — 평단 낮아져도 깊이·바닥 기준은 *최초가* 고정
        if pos.dca_initial_entry <= 0:
            pos.dca_initial_entry = pos.entry_price
            pos.dca_base_krw = float(pos.krw_spent or 0) or (pos.qty * pos.entry_price)
        initial = pos.dca_initial_entry
        max_steps = int(float(getattr(cfg, "dca_max_depth_pct", 4.0)) / step) if step > 0 else 0
        drop = (initial - price) / initial * 100.0 if initial > 0 else 0.0
        next_level = (pos.dca_count + 1) * step
        if not (pos.dca_count < max_steps and drop >= next_level and price < initial):
            return False
        # 사이즈 = 최초원금 × add_ratio × 피라미딩 배율
        pyr = min(1.0 + pos.dca_count * float(getattr(cfg, "dca_pyramid_step", 0.20)),
                  float(getattr(cfg, "dca_pyramid_max", 2.5)))
        add_krw = float(pos.dca_base_krw or pos.krw_spent or 0) * float(getattr(cfg, "dca_add_ratio", 0.25)) * pyr
        # 예산 cap — 한 코인 총투입 ≤ 슬롯예산 × mult (over-allocation 차단)
        cap = self._effective_budget() * float(getattr(cfg, "dca_max_pos_mult", 3.0))
        if cap > 0 and (float(pos.krw_spent or 0) + add_krw) > cap:
            add_krw = max(0.0, cap - float(pos.krw_spent or 0))
        min_krw = getattr(self.client, "MIN_ORDER_KRW", 5000.0)
        if add_krw < min_krw:
            return False   # 예산 소진 → 더 못 탐, 존버로 자연 인계
        # ★ 떨어지는 칼 게이트 — 직전 5M봉 급락 중이면 이번 tick 물타기 보류(바닥 다질 때까지).
        #   freefall 칼받기로 사이즈만 키워 SL/존버 손익비 역전되던 것 차단(효자 눌림목 DCA는 통과).
        try:
            from app.manager.spot_entry_guards import check_dca_stabilized
            _stab_ok, _stab_why = check_dca_stabilized(self.client, pos.market, cfg)
            if not _stab_ok:
                logger.info("[SPOT_GAZUA] %s 물타기 보류 — %s", pos.market, _stab_why)
                return False
        except Exception as _stab_exc:
            logger.debug("[SPOT_GAZUA] dca_stabilize fail-open: %s", _stab_exc)
        return self._book_addbuy(pos, price, add_krw)

    def _book_addbuy(self, pos: "SpotGazuaPosition", ref_price: float, add_krw: float) -> bool:
        """물타기 1회 체결을 *장부에* 반영 — qty/krw_spent/평단 갱신 + 타깃 재계산 +
        (live) 서버측 TP 취소·재배치 + 저널 'DCA'(비청산). ★불변식: 평단 = 총원금 ÷ 총수량."""
        add_price = ref_price
        add_qty = add_krw / add_price if add_price > 0 else 0.0
        if add_qty <= 0:
            return False
        if pos.paper:
            # ★ [2026-06-24] paper 슬리피지 — DCA 매수도 불리(비싸게) 체결 가정 → averaged 평단 현실화.
            _slip = max(0.0, float(getattr(self.config, "paper_slippage_bps", 0.0))) / 10000.0
            if _slip > 0 and add_price > 0:
                add_price *= (1.0 + _slip)
                add_qty = add_krw / add_price
            logger.info("[SPOT_GAZUA][PAPER] DCA#%d %s +krw=%.0f qty=%.8f @ %.4f",
                        pos.dca_count + 1, pos.market, add_krw, add_qty, add_price)
        else:
            try:
                od = self.client.market_buy(pos.market, add_krw)
                uuid = str(od.get("uuid", "") or "")
                exq = float(od.get("executed_volume", 0) or 0)
                fp = float(od.get("avg_price", 0) or 0)
                if uuid:
                    try:
                        od2 = self.client.wait_order(uuid=uuid, market=pos.market, timeout_sec=10.0, poll_interval=0.5)
                        exq = float(od2.get("executed_volume", 0) or 0) or exq
                        fp = float(od2.get("avg_price", 0) or 0) or fp
                    except Exception as q_exc:
                        logger.warning("[SPOT_GAZUA] DCA wait_order %s 실패: %s", pos.market, q_exc)
                if exq > 0:
                    add_qty = exq
                if fp > 0:
                    add_price = fp
                add_krw = add_qty * add_price   # 실체결 기준 원금 재확정
                logger.info("[SPOT_GAZUA] DCA#%d %s +krw=%.0f qty=%.8f @평단 %.4f",
                            pos.dca_count + 1, pos.market, add_krw, add_qty, add_price)
            except Exception as exc:
                logger.error("[SPOT_GAZUA] DCA BUY FAILED %s: %s", pos.market, exc)
                return False
        # ★ 회계 갱신 (불변식: 평단 = 총원금/총수량)
        new_qty = pos.qty + add_qty
        new_cost = float(pos.krw_spent or 0) + add_krw
        if new_qty <= 0:
            return False
        pos.qty = new_qty
        pos.krw_spent = new_cost
        pos.entry_price = new_cost / new_qty
        pos.dca_count += 1
        # 새 평단으로 TP/SL 재계산
        atr = self._estimate_atr(pos.market, pos.entry_price)
        t = self._compute_targets(pos.entry_price, atr)
        pos.tp1, pos.tp2, pos.sl, pos.atr_used = t.tp1, t.tp2, t.sl, t.atr_used
        # live: 평단/수량 바뀜 → 서버측 TP 주문 취소 + 재배치 (안 하면 옛 수량 매도)
        if not pos.paper:
            for uid in (pos.tp1_order_uuid, pos.tp2_order_uuid):
                if uid:
                    try:
                        self.client.cancel_order(uuid=uid, market=pos.market)
                    except Exception:
                        pass
            pos.tp1_order_uuid = ""
            pos.tp2_order_uuid = ""
            self._place_tp_orders(pos)
        self._record_journal("DCA", pos, add_price, qty=add_qty,
                             reason=f"물타기#{pos.dca_count} +₩{add_krw:.0f} 평단→{pos.entry_price:.4f}")
        self._save_state()
        return True

    def _dca_abs_floor_breached(self, pos: "SpotGazuaPosition", price: float) -> bool:
        """최초진입가 기준 절대 바닥(dca_abs_sl_pct) 이하 = 존버 무시 강제매도 신호.
        0(또는 양수) = 무제한 존버(바닥 없음)."""
        floor_pct = float(getattr(self.config, "dca_abs_sl_pct", 0.0) or 0.0)
        if floor_pct >= 0:
            return False
        initial = pos.dca_initial_entry or pos.entry_price
        if initial <= 0:
            return False
        return price <= initial * (1.0 + floor_pct / 100.0)

    def _maybe_release_longhold(self, pos: "SpotGazuaPosition", price: float) -> None:
        """존버 코인이 진입가+해제임계 회복 시 존버 해제 → 정상 관리 복귀."""
        if not pos.longhold_active or pos.entry_price <= 0:
            return
        profit_pct = (price / pos.entry_price - 1) * 100.0
        if profit_pct >= self._longhold_release_pct(pos):
            pos.longhold_active = False
            logger.info("[SPOT_GAZUA] 🔓 존버 해제 %s @%.4f (+%.2f%% 회복) — 정상 관리 복귀",
                        pos.market, price, profit_pct)
            self._record_journal("LONGHOLD_RELEASE", pos, price, reason=f"+{profit_pct:.2f}% 회복")
            self._save_state()

    # ── ② 청산가드: multi_be_lock (peak 단계별 SL 위로 잠금) ──────
    def _apply_multi_be_lock(self, pos: "SpotGazuaPosition") -> None:
        """peak 이익이 단계 넘을 때마다 SL 을 위로만 잠금(ratchet). 손실 중엔 미발동.
        ★ 보호용 — 절대 SL 을 내리지 않음(max). 잠금레벨 고정(BE+cushion / +0.3 / +1.0 / +2.0%)."""
        cfg = self.config
        if not cfg.multi_be_lock_enabled or pos.entry_price <= 0:
            return
        e = pos.entry_price
        peak_pct = (pos.trailing_high / e - 1) * 100.0 if pos.trailing_high > 0 else 0.0
        # ★ ATR 적응 arming floor — 메이저(저변동)는 0.25% peak 가 노이즈라 즉시 BE락→노이즈 컷(Bybit 회전매).
        #   ON 이면 ATR% 노이즈 위에서만 be_lock 시작 → 잠금 시 price 가 SL 위로 충분히 떠 노이즈 컷 방지.
        _arm = cfg.multi_be_lock_stage1_pct
        if getattr(cfg, "multi_be_lock_atr_adaptive", False) and pos.atr_used > 0:
            _atr_pct = pos.atr_used / e * 100.0
            _arm = max(_arm, _atr_pct * float(getattr(cfg, "multi_be_lock_atr_mult", 2.0)))
        if peak_pct < _arm:
            return   # 1단계(또는 ATR 노이즈 floor) 전 — 원 SL 유지(손실 중 포함)
        if peak_pct >= cfg.multi_be_lock_stage4_pct:
            target, lvl = e * 1.02, "+2.0%"
        elif peak_pct >= cfg.multi_be_lock_stage3_pct:
            target, lvl = e * 1.01, "+1.0%"
        elif peak_pct >= cfg.multi_be_lock_stage2_pct:
            target, lvl = e * 1.003, "+0.3%"
        else:   # stage1 → 본전 + 수수료 쿠션
            target, lvl = e * (1 + cfg.multi_be_lock_fee_cushion_pct / 100.0), "BE"
        if target > pos.sl:   # 위로만 (ratchet)
            pos.sl = round(target, 8)
            logger.info("[SPOT_GAZUA] BE락 %s peak%.2f%% → SL↑ %s(%.4f)",
                        pos.market, peak_pct, lvl, pos.sl)
            self._save_state()

    # ── ② 청산가드: be_stall intelligent (peak 정체 + 모멘텀 꺾임 → 익절 컷) ──
    def _check_be_stall(self, pos: "SpotGazuaPosition", price: float) -> Optional[str]:
        """peak 근처 정체 + 모멘텀 반대 확실 → 익절 컷 사유 반환(아니면 None).
        5중 안전장치: 시간윈도우·수수료가드·모멘텀·중립보수·손실/존버 미발동. (DESIGN §2.2)"""
        cfg = self.config
        if not cfg.be_stall_enabled or pos.longhold_active or pos.entry_price <= 0:
            return None
        peak_pct = (pos.trailing_high / pos.entry_price - 1) * 100.0 if pos.trailing_high > 0 else 0.0
        pnl_pct = (price / pos.entry_price - 1) * 100.0
        last_peak = pos.last_peak_ts or pos.entry_ts
        stall_sec = time.time() - last_peak
        # ① 시간 윈도우 [min, max] — stale peak 컷오프
        if not (cfg.be_stall_sec <= stall_sec <= cfg.be_stall_max_since_peak_sec):
            return None
        # ② 수수료 가드 + ★손실 구간 미발동 — be_stall 은 익절(peak 반납 방지) 가드다.
        #   현재 pnl 이 수익(≥0.15%, 수수료 위)일 때만 컷. ★옛 'or peak≥0.30%' 구멍 제거:
        #   peak 한번 넘었다고 *현재 손실*(-%)에 발동하면 DCA 효자방식을 죽이고 회전매가 됨
        #   (2026-06-21 AXS: peak+0.49% pnl-0.95% 에 발동→물타기 직후 손절→재진입 반복 9회/48분).
        #   손실 구간 청산은 DCA→존버(longhold)→abs_sl(-35%)가 전담. (DESIGN §2.2)
        if pnl_pct < 0.15:
            return None
        # ③ intelligent 모멘텀 (5m)
        try:
            raw5 = self.client.get_kline(pos.market, interval="5", limit=40)
            closes5 = [float(r[4]) for r in raw5 if len(r) >= 5]
        except Exception:
            closes5 = []
        from app.manager.spot_exit_guards import score_momentum_long
        for_s, against_s, detail = score_momentum_long(
            closes5, rsi_strong=cfg.be_stall_rsi_strong, rsi_weak=cfg.be_stall_rsi_weak,
        )
        if for_s >= 2 and against_s == 0:
            pos.last_peak_ts = time.time()   # 우리편 → 보유 + 타이머 리셋
            return None
        against_clear = (against_s >= 2 and for_s == 0)
        # ④ 중립 폴백 = 보수 HOLD (neutral_exit=False 면 컷 안 함)
        if not against_clear and not cfg.be_stall_neutral_exit:
            pos.last_peak_ts = time.time()   # 중립 → 보유(타이머 리셋)
            return None
        kind = "intel" if against_clear else "time"
        return f"be_stall {kind} {int(stall_sec)}s peak+{peak_pct:.2f}% pnl+{pnl_pct:.2f}% ({detail})"

    def _sell_all(self, pos: SpotGazuaPosition, reason: str) -> bool:
        """전량 매도. 성공 True / 실패 False(포지션 유지·재시도용)."""
        if pos.paper:
            logger.info("[SPOT_GAZUA][PAPER] SELL ALL %s qty=%.8f (%s)", pos.market, pos.qty, reason)
            logger.info("[SPOT_GAZUA] CLOSE %s reason=%s", pos.market, reason)
            return True
        try:
            self.client.market_sell(pos.market, pos.qty)
            logger.info("[SPOT_GAZUA] CLOSE %s reason=%s", pos.market, reason)
            return True
        except Exception as exc:
            # insufficient_funds_ask: 기록 qty > 실보유(수수료/반올림). 실보유 조회 후 재시도.
            if "insufficient" in str(exc).lower():
                try:
                    from app.integrations.upbit_trade import base_currency
                    bal = float(self.client.get_balance(base_currency(pos.market)))
                    if bal > 0:
                        self.client.market_sell(pos.market, bal)
                        pos.qty = 0.0
                        logger.info("[SPOT_GAZUA] CLOSE %s reason=%s (실보유 %.8f 재시도 성공)",
                                    pos.market, reason, bal)
                        return True
                    logger.warning("[SPOT_GAZUA] %s 실보유 0 — 이미 청산됨, 포지션 정리", pos.market)
                    return True
                except Exception as e2:
                    logger.error("[SPOT_GAZUA] sell_all %s 실보유 재시도 실패: %s", pos.market, e2)
            logger.error("[SPOT_GAZUA] sell_all %s FAILED: %s", pos.market, exc)
            return False

    def _sell_partial(self, pos: SpotGazuaPosition, pct: float) -> bool:
        """부분 매도. 성공 True / 실패 False(partial_done 보류·재시도)."""
        q = pos.qty * (pct / 100.0)
        if pos.paper:
            logger.info("[SPOT_GAZUA][PAPER] SELL %.0f%% %s qty=%.8f", pct, pos.market, q)
            pos.qty -= q
            return True
        try:
            self.client.market_sell(pos.market, q)
            pos.qty -= q
            return True
        except Exception as exc:
            logger.error("[SPOT_GAZUA] partial sell %s FAILED: %s", pos.market, exc)
            return False

    # ── 🕊️ 대사면(amnesty) — 유치장 코인 입양 ──────────────────
    #   "코인은 매번 배신하지만 매번 기회를 준다" (부모님 2026-06-16)
    #   거래소엔 있지만 봇 밖에 갇힌 orphan 을 *사람이 고른 것만* 봇 관리로 꺼냄.
    #   ★ 자동 입양 절대 X — 부모님 다른 코인까지 팔아버리는 사고 방지.
    def _estimate_atr(self, market: str, ref_price: float) -> float:
        """입양 코인의 TP/SL 계산용 ATR 근사. 실패 시 평단×2%."""
        try:
            raw = self.client.get_kline(market, interval=self.config.primary_tf, limit=15)
            trs, pc = [], None
            for r in raw:
                h, l, c = float(r[2]), float(r[3]), float(r[4])
                tr = (h - l) if pc is None else max(h - l, abs(h - pc), abs(l - pc))
                trs.append(tr); pc = c
            if trs:
                return sum(trs) / len(trs)
        except Exception:
            pass
        return ref_price * 0.02

    def list_orphans(self) -> List[Dict[str, Any]]:
        """거래소 KRW 보유 중 봇 positions 에 없는 코인 = 사면 후보(정보만)."""
        out: List[Dict[str, Any]] = []
        try:
            accts = self.client.accounts()
        except Exception as exc:
            logger.warning("[SPOT_GAZUA] orphan 조회 실패: %s", exc)
            return out
        held = {p.market for p in self.positions}
        min_krw = getattr(self.client, "MIN_ORDER_KRW", 5000.0)
        for a in accts:
            cur = str(a.get("currency", "")).upper()
            if cur in (self._quote_currency, ""):
                continue
            bal = float(a.get("balance", 0) or 0)
            if bal <= 0:
                continue
            market = self._normalize_market(cur)
            if market in held:
                continue
            price = self._get_price(market) or 0.0
            value = bal * price
            if value < min_krw:
                continue  # dust = 거래소에서도 못 팖 → 사면 무의미
            avg = float(a.get("avg_buy_price", 0) or 0)
            pnl = (price / avg - 1) * 100 if avg > 0 else 0.0
            out.append({
                "market": market, "currency": cur, "balance": bal,
                "avg_buy_price": avg, "current_price": price,
                "value_krw": value, "pnl_pct": pnl,
            })
        return out

    def adopt_orphan(self, market: str) -> Dict[str, Any]:
        """사면 — 거래소 보유분을 봇 관리로 입양(평단 기준 TP/SL + 서버측 TP 박기)."""
        from app.integrations.upbit_trade import base_currency
        market = str(market).upper().strip()
        if self.config.paper:
            return {"ok": False, "error": "paper 모드 — 실거래 전환 후 사면 가능"}
        if any(p.market == market for p in self.positions):
            return {"ok": False, "error": "이미 봇이 관리 중"}
        base = base_currency(market)
        try:
            bal = float(self.client.get_balance(base))
        except Exception as exc:
            return {"ok": False, "error": f"잔고조회 실패: {exc}"}
        price = self._get_price(market) or 0.0
        min_krw = getattr(self.client, "MIN_ORDER_KRW", 5000.0)
        if bal <= 0 or price <= 0 or bal * price < min_krw:
            return {"ok": False, "error": f"보유 부족/dust (₩{bal * price:.0f})"}
        # 평단 = 계정 avg_buy_price (없으면 현재가)
        avg = 0.0
        try:
            for a in self.client.accounts():
                if str(a.get("currency", "")).upper() == base:
                    avg = float(a.get("avg_buy_price", 0) or 0)
                    break
        except Exception:
            pass
        entry = avg if avg > 0 else price
        atr = self._estimate_atr(market, entry)
        targets = self._compute_targets(entry, atr)
        pos = SpotGazuaPosition(
            market=market, direction="LONG", entry_price=entry, qty=bal,
            tp1=targets.tp1, tp2=targets.tp2, sl=targets.sl, atr_used=targets.atr_used,
            entry_ts=time.time(), trailing_high=max(entry, price), krw_spent=bal * entry,
            paper=False, order_uuid="ADOPTED",
        )
        self._place_tp_orders(pos)
        self.positions.append(pos)
        if self.state == FocusState.IDLE:
            self.state = FocusState.POSITIONED
        self._record_journal("ENTRY", pos, entry, reason="🕊️ 사면 입양")
        self._save_state()
        logger.info("[SPOT_GAZUA] 🕊️ 대사면 입양 %s qty=%.8f 평단=%.4f (sl=%.4f tp1=%.4f tp2=%.4f)",
                    market, bal, entry, targets.sl, targets.tp1, targets.tp2)
        return {"ok": True, "market": market, "entry": entry, "qty": bal,
                "sl": targets.sl, "tp1": targets.tp1, "tp2": targets.tp2}

    # ── 거래소 동기화 (수동 TP 이동 따라가기) ──────────────────
    def _sync_from_exchange(self, pos: "SpotGazuaPosition", price: float) -> None:
        """거래소 미체결 매도주문 + 잔고를 읽어 봇 tp1/tp2/qty 동기화.
        부모님이 앱에서 TP를 옮기면(취소→재주문) 봇이 그 가격으로 따라온다.
        ※ SL은 거래소에 주문이 없어(폴링 감시) 동기화 대상 아님 — 봇 내부값 유지."""
        from app.integrations.upbit_trade import base_currency
        try:
            opens = self.client.open_orders(pos.market, side="ask")
        except Exception as exc:
            logger.debug("[SPOT_GAZUA] %s open_orders 조회 실패(무시): %s", pos.market, exc)
            return
        sells = []
        for o in (opens or []):
            try:
                pr = float(o.get("price") or 0)
                uid = str(o.get("uuid") or "")
                if pr > 0 and uid:
                    sells.append((pr, uid))
            except (TypeError, ValueError):
                continue
        sells.sort(key=lambda x: x[0])  # 가격 오름차순: 낮은=TP1, 높은=TP2
        changed = False
        if len(sells) >= 2:
            (p1, u1), (p2, u2) = sells[0], sells[-1]
            if abs(p1 - pos.tp1) > 1e-9 or u1 != pos.tp1_order_uuid:
                changed = True
            if abs(p2 - pos.tp2) > 1e-9 or u2 != pos.tp2_order_uuid:
                changed = True
            pos.tp1, pos.tp1_order_uuid = p1, u1
            pos.tp2, pos.tp2_order_uuid = p2, u2
        elif len(sells) == 1:
            p2, u2 = sells[0]
            if abs(p2 - pos.tp2) > 1e-9 or u2 != pos.tp2_order_uuid or pos.tp1_order_uuid:
                changed = True
            pos.tp2, pos.tp2_order_uuid = p2, u2
            # ★ TP1 슬롯 사라짐: 그냥 비우면 체결(부분익절)을 놓침(저널·원금 미반영 버그).
            #   체결이면 장부 반영(_book_partial), 수동취소/병합이면 슬롯만 비움.
            if pos.tp1_order_uuid and not pos.partial_done:
                try:
                    od1 = self.client.get_order(uuid=pos.tp1_order_uuid, market=pos.market)
                    if str(od1.get("state", "")).lower() == "done":
                        self._book_partial(pos, pos.tp1, self.config.partial_pct / 100.0,
                                           sold_qty=float(od1.get("executed_volume", 0) or 0))
                        pos.sl = max(pos.sl, pos.entry_price)
                        logger.info("[SPOT_GAZUA] %s TP1 체결 감지(sync) — 절반 익절 장부 반영", pos.market)
                        changed = True
                except Exception:
                    pass
            pos.tp1_order_uuid = ""   # 슬롯 비움(체결 기록됐거나 수동취소/병합)
        # 매도주문 0개면 손대지 않음 — 체결/취소 판단은 _manage_live_tp_orders 가 처리
        # 잔고로 qty 동기화 — ★[2026-06-21] *하향만*. 잔고가 봇 추정보다 적을 때만(외부/부분 매도
        #   감지) qty 축소. 잔고가 *많을* 때 흡수 금지: 봇이 안 산 코인(orphan·수동매수·회전매 찌꺼기)을
        #   qty 에 더하면 krw_spent(원금)는 그대로라 평단 = 원금÷부푼qty 로 붕괴(예 AXS 1876→1050) →
        #   청산 시 (매도가−가짜평단)×부푼qty = 가짜 폭리 기록(PnL 뻥튀기 근본). 외부 보유는 대사면(adopt)으로.
        try:
            bal = float(self.client.get_balance(base_currency(pos.market), include_locked=True))
            if 0 < bal < pos.qty * 0.99:
                pos.qty = bal
                changed = True
        except Exception:
            pass
        # ★ orphaned 부분익절 치유 — TP1 슬롯 비었는데 partial_done=False + TP2만 살아있고
        #   원금(krw_spent)이 보유수량 대비 과대(=옛 버그로 TP1 체결 미기록) → 실제 판 비율로 1회 보정.
        if (not pos.partial_done and not pos.tp1_order_uuid and pos.tp2_order_uuid
                and pos.entry_price > 0 and pos.qty > 0):
            implied = float(pos.krw_spent or 0) / pos.entry_price       # 원금 기준 원래 수량
            if implied > pos.qty * 1.25:                                # 보유보다 25%+ 과대 = 부분 체결 미기록
                sold_frac = max(0.0, min(0.95, 1.0 - pos.qty / implied))
                logger.info("[SPOT_GAZUA] %s orphaned 부분익절 감지(%.0f%%) — 장부 보정", pos.market, sold_frac * 100)
                self._book_partial(pos, pos.tp1 or pos.entry_price, sold_frac)
                changed = True
        if changed:
            logger.info("[SPOT_GAZUA] %s 거래소 동기화 — tp1=%.4f tp2=%.4f qty=%.8f",
                        pos.market, pos.tp1, pos.tp2, pos.qty)
            self._save_state()

    # ── 서버측 지정가 TP (폴링 스파이크 면역) ──────────────────
    def _place_tp_orders(self, pos: SpotGazuaPosition) -> None:
        """진입 직후 TP1(절반)/TP2(나머지) 지정가 매도를 거래소에 미리 박는다.
        실패 시 uuid 비워 폴링 청산으로 자동 대체(fail-safe)."""
        min_krw = getattr(self.client, "MIN_ORDER_KRW", 5000.0)
        half = pos.qty * (self.config.partial_pct / 100.0)
        rest = pos.qty - half
        try:
            if half * pos.tp1 >= min_krw and rest * pos.tp2 >= min_krw:
                od1 = self.client.limit_sell(pos.market, pos.tp1, half)
                pos.tp1_order_uuid = str(od1.get("uuid", "") or "")
                od2 = self.client.limit_sell(pos.market, pos.tp2, rest)
                pos.tp2_order_uuid = str(od2.get("uuid", "") or "")
                logger.info("[SPOT_GAZUA] 서버측 TP 박음 %s: TP1 %.4f×%.6f + TP2 %.4f×%.6f",
                            pos.market, pos.tp1, half, pos.tp2, rest)
            else:
                # 절반이 최소주문(5000) 미달 → 전량 TP2 한 방
                od2 = self.client.limit_sell(pos.market, pos.tp2, pos.qty)
                pos.tp2_order_uuid = str(od2.get("uuid", "") or "")
                logger.info("[SPOT_GAZUA] 서버측 TP(전량 TP2) 박음 %s: %.4f×%.6f",
                            pos.market, pos.tp2, pos.qty)
        except Exception as exc:
            pos.tp1_order_uuid = ""
            pos.tp2_order_uuid = ""
            logger.error("[SPOT_GAZUA] TP 지정가 주문 실패 %s: %s — 폴링 청산으로 대체", pos.market, exc)

    def _manage_live_tp_orders(self, pos: SpotGazuaPosition, price: float) -> bool:
        """live: 서버측 TP 주문 체결 확인 + SL 폴링. 전량 종료 시 True."""
        # 1) TP1 체결 → 절반 익절 + SL 본전
        if pos.tp1_order_uuid and not pos.partial_done:
            try:
                od = self.client.get_order(uuid=pos.tp1_order_uuid, market=pos.market)
                if str(od.get("state", "")).lower() == "done":
                    filled = float(od.get("executed_volume", 0) or 0)
                    self._book_partial(pos, pos.tp1, self.config.partial_pct / 100.0, sold_qty=filled)  # ★저널+원금분할
                    pos.qty = max(0.0, pos.qty - filled)
                    pos.sl = max(pos.sl, pos.entry_price)  # 본전 이동 (ratchet — be_lock 안 내림)
                    pos.tp1_order_uuid = ""
                    logger.info("[SPOT_GAZUA] TP1 체결 %s — 절반 익절(%.6f), SL→본전 %.4f",
                                pos.market, filled, pos.sl)
                    self._save_state()
            except Exception as exc:
                logger.warning("[SPOT_GAZUA] TP1 주문조회 %s 실패: %s", pos.market, exc)
        # 2) TP2 체결 → 전량 청산 완료
        if pos.tp2_order_uuid:
            try:
                od = self.client.get_order(uuid=pos.tp2_order_uuid, market=pos.market)
                if str(od.get("state", "")).lower() == "done":
                    logger.info("[SPOT_GAZUA] TP2 체결 %s — 전량 청산 완료", pos.market)
                    self._record_journal("EXIT", pos, pos.tp2, reason="TP2 체결")
                    pos.tp2_order_uuid = ""
                    return True
            except Exception as exc:
                logger.warning("[SPOT_GAZUA] TP2 주문조회 %s 실패: %s", pos.market, exc)
        # 3) SL 폴링 (Upbit 현물 stop 미지원) → 닿으면 미체결 TP 취소 + 잔량 시장가 매도
        if price <= pos.sl:
            # ★ §4.2: A 중재 — 존버 자격이면 매도 보류(TP 지정가 유지=회복 시 익절).
            if not self._resolve_sl_exit(pos, price, f"SL hit: {price:.4f} <= {pos.sl:.4f}"):
                return False
            for uid in (pos.tp1_order_uuid, pos.tp2_order_uuid):
                if uid:
                    try:
                        self.client.cancel_order(uuid=uid, market=pos.market)
                    except Exception as c_exc:
                        logger.warning("[SPOT_GAZUA] TP 주문취소 %s 실패: %s", pos.market, c_exc)
            pos.tp1_order_uuid = ""
            pos.tp2_order_uuid = ""
            if self._sell_all(pos, f"SL hit: {price:.4f} <= {pos.sl:.4f}"):
                self.daily_sl_count += 1
                self._record_journal("EXIT", pos, price, reason="SL hit")
                return True
            pos.close_retry_count += 1
            if pos.close_retry_count >= 5:
                logger.error("[SPOT_GAZUA] %s SL 매도 5회 실패 — 수동 정리 필요(orphan)", pos.market)
                return True
            self._save_state()
            return False
        return False

    # ── 예산 / 가격 ─────────────────────────────────────────
    def _effective_budget(self) -> float:
        """슬롯당 예산 = 총자산 ÷ max_positions (여러 코인 균등 분산).
        총자산(equity) = 자동(budget=0): 가용 KRW + 보유 포지션 원금(krw_spent)
                       / 수동(budget>0): 그 고정 총액.
        ★ equity 에 보유분(krw_spent)을 포함하므로 슬롯이 차도 equity 일정
          → per_slot 이 늘 ~1/N 로 유지 = 진짜 균등 분산 (남은잔고 기준이면 점점 작아짐).
        실제 가용 KRW 한도로 cap(현물=잔고 이상 못 삼) + 99.5%(수수료·슬리피지 버퍼,
        KRW-XLM 200138 insufficient_funds 교훈)."""
        slots = max(1, int(self.config.max_positions))
        try:
            if self.config.paper:
                equity = 1_000_000.0                                   # paper 가상 100만
                held = sum(float(p.krw_spent or 0) for p in self.positions)
                free = max(0.0, equity - held)
            else:
                free = float(self.client.get_balance(self._quote_currency))
                held = sum(float(p.krw_spent or 0) for p in self.positions)
                equity = (float(self.config.budget) if self.config.budget > 0
                          else free + held)
            per_slot = equity / slots
            return max(0.0, min(per_slot, free) * 0.995)
        except Exception:
            return 0.0

    def _conv_size_factor(self, conf01: float) -> float:
        """confidence(0~1) → 슬롯 상한 대비 사이즈 배율(floor~1.0).
        점수 비례 사이징: 통과 하한(entry_conf_threshold) 신호=floor, 만점(1.0)=슬롯 가득.
        선형 정규화 [lo,1.0]→[floor,1.0]. conv_sizing_enabled=False 면 1.0(균등 1/N).
        Bybit conviction-weighted sizing(_compute_entry_budget) 의 Upbit 판."""
        cfg = self.config
        if not cfg.conv_sizing_enabled:
            return 1.0
        floor_f = max(0.0, min(1.0, float(cfg.conv_size_floor)))
        lo = float(cfg.entry_conf_threshold)
        if lo >= 1.0:
            return 1.0
        t = max(0.0, min(1.0, (float(conf01) - lo) / (1.0 - lo)))
        return floor_f + (1.0 - floor_f) * t

    def _get_price(self, market: str) -> float:
        try:
            return float(self.client.get_price(market))
        except Exception:
            return 0.0

    # ── 일일 카운터 ─────────────────────────────────────────
    @staticmethod
    def _trading_day(ts: Optional[float] = None) -> str:
        """거래일 기점 = 07:00 KST (Bybit 동일). 07시 전 거래는 전일로 귀속.
        서버 localtime=KST 이므로 -7h 오프셋 후 날짜 = 07시 경계."""
        t = time.time() if ts is None else float(ts or 0)
        return time.strftime("%Y-%m-%d", time.localtime(t - 7 * 3600))

    def _maybe_reset_daily(self) -> None:
        # ★ 07:00 KST 기점 (Bybit 통일). 옛 gmtime(UTC자정=09시 KST) → 07시로.
        stamp = self._trading_day()
        if stamp != self._day_stamp:
            self._day_stamp = stamp
            self.daily_plans_used = 0
            self.daily_sl_count = 0

    # ── 영속화 (원자적 쓰기) ────────────────────────────────
    def _save_state(self) -> None:
        try:
            data = {
                "config": asdict(self.config),
                "state": {
                    "focus_state": self.state.value,
                    "positions": [p.to_dict() for p in self.positions],
                    "daily_plans_used": self.daily_plans_used,
                    "daily_sl_count": self.daily_sl_count,
                    "day_stamp": self._day_stamp,
                    "cooldown_until": self.cooldown_until,
                    "paper_seq": self._paper_seq,
                },
            }
            tmp = self.state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.state_path)
        except Exception as exc:
            logger.error("[SPOT_GAZUA] save_state FAILED: %s", exc)

    def _load_state(self) -> None:
        try:
            if not os.path.exists(self.state_path):
                return
            with open(self.state_path, encoding="utf-8") as f:
                data = json.load(f)
            cfg_in = data.get("config") or {}
            for k, v in cfg_in.items():
                if hasattr(self.config, k):
                    setattr(self.config, k, v)
            # ★ budget_krw → budget 리네임 마이그레이션 (옛 state 호환·설정값 보존). 통화 중립 이름.
            if "budget_krw" in cfg_in and not cfg_in.get("budget"):
                try:
                    self.config.budget = float(cfg_in.get("budget_krw") or 0.0)
                except (TypeError, ValueError):
                    pass
            # ── config 마이그레이션 (옛 runtime 의 stale OFF값이 새 ON 기본을 덮는 것 1회 교정) ──
            #   2026-06-17 부모 "모든 것 기본 ON, paper만 빼고". 옛 파일엔 config_version 없음(=0) →
            #   진입품질/존버 게이트를 현재 dataclass 기본(ON)으로 강제 1회 적용 후 버전 올림.
            #   ※ 이후엔 v1 이라 재발동 안 함 — 부모님이 UI 로 조정한 값 보존.
            loaded_ver = int(cfg_in.get("config_version", 0) or 0)
            _CUR_VER = SpotGazuaConfig().config_version  # 현재 코드 버전(2)
            if loaded_ver < _CUR_VER:
                _d = SpotGazuaConfig()
                # 각 버전에서 새로 ON 된 필드만 누적 강제(부모 UI 조정값은 해당 버전 이후 보존).
                _bump_fields = []
                if loaded_ver < 1:   # v1: 진입품질/존버 게이트 ON
                    _bump_fields += ["longhold_enabled", "headroom_gate_pct", "atr_sl_floor_mult",
                                     "overext_range_pos_pct", "blowoff_move_pct"]
                if loaded_ver < 2:   # v2: guard_score 표시 ON
                    _bump_fields += ["guard_score_mode_enabled"]
                if loaded_ver < 3:   # v3: guard_score 문턱 게이트 + 80 캡 ON
                    _bump_fields += ["guard_score_threshold", "guard_score_total_cap"]
                if loaded_ver < 4:   # v4: multi_be_lock(이익 보호 SL 잠금) ON
                    _bump_fields += ["multi_be_lock_enabled"]
                if loaded_ver < 5:   # v5: be_stall intelligent(모멘텀 정밀 컷) ON
                    _bump_fields += ["be_stall_enabled"]
                if loaded_ver < 7:   # v7: guard_score 문턱 45→50 (미니백테 근거·소수정예)
                    _bump_fields += ["guard_score_threshold"]
                if loaded_ver < 8:   # v8: 선물 ON 가드 복사분 default ON (부모 "FOCUS ON된 것 현물도 그대로 ON")
                    _bump_fields += ["gap_check_enabled", "micro_1m_check_enabled", "momentum_reversal_enabled",
                                     "raw_body_enabled", "mtf_align_enabled", "entry_expectation_enabled",
                                     "microtiming_5m_enabled"]
                if loaded_ver < 9:   # v9: micro_1m 노이즈 도지 면제(0.05) — 색깔만 보고 미세 도지 차단하던 가짜차단 교정
                    _bump_fields += ["micro_1m_body_min_pct"]
                if loaded_ver < 10:  # v10: 현물 GAZUA Live 전환 — paper→False 강제 (부모 "3대 전부 Live", 효자방식 DCA 실탄). 이후 서버별 UI 로 paper 복귀 보존.
                    _bump_fields += ["paper"]
                if loaded_ver < 11:  # v11: live 자동진입 위해 캔들 타이밍 게이트 OFF + ADX 문턱 완화 (부모 2026-06-21 "과차단이 진입불가까지 만들면 잡을 코인이 없다"). 전 서버 재시작 시 적용. 이후 UI 재튜닝 보존.
                    _bump_fields += ["gap_check_enabled", "micro_1m_check_enabled", "momentum_reversal_enabled",
                                     "raw_body_enabled", "mtf_align_enabled", "microtiming_5m_enabled", "min_adx_entry"]
                if loaded_ver < 12:  # v12: gate_ledger 관제판 default ON (부모 2026-06-21 "near-miss 관제판 붙이자"). 관측만·진입 불침·로컬. 이후 UI 토글 보존.
                    _bump_fields += ["gate_ledger_enabled"]
                for k in _bump_fields:
                    setattr(self.config, k, getattr(_d, k))
                self.config.config_version = _CUR_VER
                logger.info("[SPOT_GAZUA] config v%d→v%d 마이그레이션 — 기본 ON 적용: %s",
                            loaded_ver, _CUR_VER, ", ".join(_bump_fields) or "(none)")
                self._migrated_v1 = True   # __init__ 끝에서 저장 (플래그명 유지)
            st = data.get("state") or {}
            fs = st.get("focus_state", "IDLE")
            self.state = FocusState(fs) if fs in FocusState._value2member_map_ else FocusState.IDLE
            self.positions = [SpotGazuaPosition.from_dict(p) for p in st.get("positions", [])]
            # ★ 모드 정합: LIVE 모드인데 가상(paper) 포지션이 섞여 있으면 제거(실제 없음)
            if not self.config.paper:
                _before = len(self.positions)
                self.positions = [p for p in self.positions if not p.paper]
                if len(self.positions) < _before:
                    logger.info("[SPOT_GAZUA] LIVE 모드 — 로드된 가상(paper) 포지션 %d개 제거",
                                _before - len(self.positions))
            self.daily_plans_used = int(st.get("daily_plans_used", 0) or 0)
            self.daily_sl_count = int(st.get("daily_sl_count", 0) or 0)
            self._day_stamp = st.get("day_stamp", "")
            self.cooldown_until = float(st.get("cooldown_until", 0.0) or 0.0)
            self._paper_seq = int(st.get("paper_seq", 0) or 0)
            logger.info("[SPOT_GAZUA] state loaded: %d positions, %d plans used (paper=%s, enabled=%s)",
                        len(self.positions), self.daily_plans_used, self.config.paper, self.config.enabled)
        except Exception as exc:
            logger.error("[SPOT_GAZUA] load_state FAILED: %s", exc)

    # ── 거래 저널 (JSONL append-only) ──────────────────────────
    def _book_partial(self, pos: "SpotGazuaPosition", exit_price: float,
                      sold_fraction: float, sold_qty: Optional[float] = None) -> None:
        """부분익절(TP1) 1회를 *장부에* 반영 — 저널 EXIT(판 비율 원금만) + 남은 원금 차감 + partial_done.
        ★ 이중집계 방지: 판 비율(sold_fraction)만큼 krw_spent 차감 → 이후 TP2/전량 청산은 남은 원금만 PnL.
        ★ 중복 가드(partial_done): sync/manage_live/폴링 어디서 불려도 1회만. qty/SL/uuid 는 호출부가 처리."""
        if pos.partial_done:
            return
        sf = max(0.0, min(1.0, float(sold_fraction)))
        part_cost = float(pos.krw_spent or 0) * sf
        self._record_journal("EXIT", pos, exit_price, reason="TP1 부분익절",
                             qty=sold_qty, cost_override=part_cost)
        pos.krw_spent = max(0.0, float(pos.krw_spent or 0) - part_cost)
        pos.partial_done = True
        self._save_state()

    def _record_journal(self, event: str, pos: "SpotGazuaPosition", price: float,
                        reason: str = "", qty: Optional[float] = None,
                        cost_override: Optional[float] = None) -> None:
        """진입/청산 1건을 저널에 기록. 실패해도 거래엔 영향 없음(best-effort).
        현물 long_only: ROE% = 가격변동률. pnl_krw = 원금(krw_spent)×ROE.
        ★ cost_override: 부분익절은 *판 비율만큼*의 원금으로 PnL 계산(전체 krw_spent 쓰면 이중집계)."""
        try:
            q = float(qty if qty is not None else pos.qty)
            entry = float(pos.entry_price or 0)
            is_exit = (event == "EXIT" and entry > 0)
            # ★★ [2026-06-23 fix] 재진입 쿨다운(v2) 기준시각을 *모든 청산 경로*에서 박는다.
            #   기존엔 _manage_all_positions(1759)에서만 set → LIVE SL/TP 청산은
            #   _manage_live_tp_orders/_resolve_sl_exit 경로라 누락 → 45분 쿨다운이 LIVE 청산을
            #   못 봐 SL 직후 재진입 회전매(LAYER 실측). _record_journal 은 전 청산이 거치는 단일
            #   funnel 이라 여기서 박으면 paper/live·SL/TP/수동 전부 커버.
            if event == "EXIT":
                try:
                    self._recent_exit[pos.market] = time.time()
                except Exception:
                    pass
            # ★ PnL₩ = 원금(krw_spent) × 가격변동률 − 왕복 수수료 (net, 부모님 입력 fee_rate_pct 2026-06-17).
            #   옛 (청산가-평단)×qty 는 거래소 잔고동기화로 qty 오염 시 ₩0/과대값 → 원금 기준이 안정적.
            #   수수료: 매수측 = 원금×율, 매도측 = 매도대금(원금×ratio)×율. 둘 다 차감해야 실제 net.
            cost = float(cost_override) if cost_override is not None else float(pos.krw_spent or 0)
            fee_r = max(0.0, float(getattr(self.config, "fee_rate_pct", 0.0))) / 100.0
            if is_exit:
                # ★ [2026-06-24] paper 슬리피지 — 매도는 불리하게(싸게) 체결 가정. 전 청산이 이 funnel 을
                #   거치므로 SL/TP/수동/부분 전부 커버. LIVE 는 price=실체결가라 무관(slip 미적용).
                if bool(getattr(self.config, "paper", False)):
                    _eslip = max(0.0, float(getattr(self.config, "paper_slippage_bps", 0.0))) / 10000.0
                    if _eslip > 0:
                        price = price * (1.0 - _eslip)
                ratio = price / entry
                gross_krw = cost * (ratio - 1.0)
                fee_krw = cost * fee_r + (cost * ratio) * fee_r   # 매수 + 매도 왕복
                pnl_krw = round(gross_krw - fee_krw, 2)
                roe = round((pnl_krw / cost) * 100, 3) if cost > 0 else 0.0
            else:
                gross_krw, fee_krw, pnl_krw, roe = 0.0, 0.0, 0.0, 0.0
            rec = {
                "ts": time.time(), "strategy": "FOCUS", "event": event,
                "market": pos.market, "direction": "LONG",
                "entry": round(entry, 8), "exit": round(price, 8) if event == "EXIT" else None,
                "qty": round(q, 8), "pnl_krw": pnl_krw, "roe_pct": roe,
                "gross_pnl_krw": round(gross_krw, 2), "fee_krw": round(fee_krw, 2),
                "hold_sec": round(time.time() - pos.entry_ts, 1),
                "reason": reason, "paper": bool(pos.paper),
            }
            # ★ [2026-06-20] 락 + flush + fsync — 동시 write/삭제 race·크래시 부분기록(조각 레코드)으로
            #   기록 누락되던 것 차단(선물 TradeJournal 패턴 미러). 한 줄 통째 write.
            line = json.dumps(rec, ensure_ascii=False) + "\n"
            with self._journal_lock:
                with open(self.journal_path, "a", encoding="utf-8") as f:
                    f.write(line)
                    f.flush()
                    os.fsync(f.fileno())
        except Exception as exc:
            logger.debug("[SPOT_GAZUA] journal write 실패(무시): %s", exc)

    def read_journal(self, limit: int = 100) -> List[Dict[str, Any]]:
        """최근 거래기록 (최신순). 파일 없으면 빈 리스트."""
        try:
            if not os.path.exists(self.journal_path):
                return []
            with self._journal_lock:
                with open(self.journal_path, encoding="utf-8") as f:
                    lines = f.readlines()
            out = []
            for ln in lines[-max(limit, 1):]:
                ln = ln.strip()
                if ln:
                    try:
                        out.append(json.loads(ln))
                    except Exception:
                        continue
            out.reverse()
            return out
        except Exception as exc:
            logger.debug("[SPOT_GAZUA] journal read 실패: %s", exc)
            return []

    def delete_journal(self, ts: float) -> Dict[str, Any]:
        """저널 기록 1건 삭제 (ts 매칭). 파일을 ts 줄만 빼고 재작성. 거래 무관(기록만)."""
        try:
            if not os.path.exists(self.journal_path):
                return {"ok": False, "error": "저널 없음"}
            # ★ [2026-06-20] 락 + atomic(temp→replace) 재작성 — 삭제 중 append 가 끼어들거나 "w" truncate 가
            #   중단되며 파일 깨지던 것 차단(조각 레코드=기록 누락의 원인 중 하나).
            with self._journal_lock:
                with open(self.journal_path, encoding="utf-8") as f:
                    lines = f.readlines()
                kept, removed = [], 0
                for ln in lines:
                    s = ln.strip()
                    if not s:
                        continue
                    try:
                        row_ts = float(json.loads(s).get("ts", 0) or 0)
                    except Exception:
                        kept.append(ln); continue           # 파싱 불가 줄은 보존
                    if abs(row_ts - float(ts)) < 1e-6:
                        removed += 1
                    else:
                        kept.append(ln)
                if removed == 0:
                    return {"ok": False, "error": "해당 기록 없음"}
                _tmp = self.journal_path + ".tmp"
                with open(_tmp, "w", encoding="utf-8") as f:
                    f.writelines(kept)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(_tmp, self.journal_path)   # atomic rename
            logger.info("[SPOT_GAZUA] journal 삭제 ts=%s (%d건)", ts, removed)
            return {"ok": True, "removed": removed}
        except Exception as exc:
            logger.warning("[SPOT_GAZUA] journal 삭제 실패: %s", exc)
            return {"ok": False, "error": str(exc)}

    def journal_summary(self, daily_days: int = 30) -> Dict[str, Any]:
        """저널 집계 — 누적/오늘 PnL, 승률, 일별 PnL(막대그래프용)."""
        rows = self.read_journal(limit=5000)
        exits = [r for r in rows if r.get("event") == "EXIT"]
        today = self._trading_day()  # ★ 거래일 기점 07:00 KST (Bybit 통일)
        total_pnl = round(sum(float(r.get("pnl_krw", 0) or 0) for r in exits), 2)
        today_pnl = 0.0
        wins = 0
        daily: Dict[str, float] = {}
        for r in exits:
            day = self._trading_day(r.get("ts"))
            pk = float(r.get("pnl_krw", 0) or 0)
            daily[day] = round(daily.get(day, 0.0) + pk, 2)
            if day == today:
                today_pnl += pk
            if pk > 0:
                wins += 1
        n = len(exits)
        days_sorted = sorted(daily.keys())[-max(daily_days, 1):]
        return {
            "total_pnl_krw": total_pnl,
            "today_pnl_krw": round(today_pnl, 2),
            "trades": n,
            "win_rate": round(wins / n * 100, 1) if n else 0.0,
            "daily": [{"day": d, "pnl_krw": daily[d]} for d in days_sorted],
        }

    # ── 계정 요약 (Overall Status 카드용, TTL 캐시) ─────────────
    def account_summary(self, ttl: float = 5.0) -> Dict[str, Any]:
        """KRW 가용 + 보유코인 평가액 + 총자산(₩). 읽기전용·캐시."""
        now = time.time()
        if self._acct_cache and (now - self._acct_cache_ts) < ttl:
            return self._acct_cache
        krw_free = 0.0
        holdings = 0.0
        try:
            # 견적통화(KRW/USDT) 가용 + 비-견적 코인은 ★티커 1회 배치조회(코인마다 개별호출 금지
            #   — LIVE 통합계좌에 코인 많으면 status 가 행걸려 'Failed to fetch' 남).
            coin_bals: Dict[str, float] = {}
            for a in self.client.accounts():
                cur = str(a.get("currency", "")).upper()
                bal = float(a.get("balance", 0) or 0) + float(a.get("locked", 0) or 0)
                if cur == self._quote_currency:
                    krw_free += bal
                elif bal > 0:
                    coin_bals[cur] = coin_bals.get(cur, 0.0) + bal
            if coin_bals:
                markets = [self._normalize_market(c) for c in coin_bals]
                price_map: Dict[str, float] = {}
                try:
                    for t in (self.client.get_tickers(markets) or []):
                        price_map[str(t.get("market", ""))] = float(t.get("trade_price", 0) or 0)
                except Exception:
                    pass
                for c, bal in coin_bals.items():
                    holdings += bal * (price_map.get(self._normalize_market(c)) or 0.0)
        except Exception as exc:
            logger.debug("[SPOT_GAZUA] account_summary 실패: %s", exc)
            return self._acct_cache or {"krw_free": 0.0, "holdings_krw": 0.0, "equity_krw": 0.0}
        out = {
            "krw_free": round(krw_free, 0),
            "holdings_krw": round(holdings, 0),
            "equity_krw": round(krw_free + holdings, 0),
        }
        self._acct_cache = out
        self._acct_cache_ts = now
        return out

    # ── 퀵 트레이드 (수동 즉시 시장가) ─────────────────────────
    def _normalize_market(self, market: str) -> str:
        """수동입력 마켓 정규화 — 거래소별 override 지점. Upbit: 'BTC'→'KRW-BTC'.
        (Bybit 현물 등 USDT 거래소는 BybitSpotGazuaManager 에서 'BTC'→'BTCUSDT' 로 override)"""
        m = str(market).upper().strip()
        if not m.startswith("KRW-"):
            m = f"KRW-{m}"
        return m

    def quick_order(self, market: str, side: str, *, krw: float = 0.0, qty: float = 0.0,
                    pct: float = 0.0) -> Dict[str, Any]:
        """대시보드 퀵트레이드 — 봇 관리와 무관한 즉시 시장가 주문.
        원 모드: 매수=KRW 금액, 매도=수량(0=실보유 전량).
        % 모드(pct>0): 매수=가용 KRW×%, 매도=실보유×% — 실잔고를 거래소에서 권위있게 환산. paper 차단."""
        from app.integrations.upbit_trade import base_currency
        market = self._normalize_market(market)
        s = str(side).lower()
        pct = max(0.0, min(100.0, float(pct or 0.0)))
        if self.config.paper:
            return {"ok": False, "error": "paper 모드 — 실거래 전환 후 퀵주문 가능"}
        try:
            if s in ("buy", "bid", "long"):
                # ★ 거래소 경고 종목 수동 매수 차단 (Entry 설정 토글). 매도는 항상 허용(빠져나오기).
                wf = {}
                try:
                    wf = self.client.get_market_warnings().get(market, {})
                except Exception:
                    pass
                if self.config.block_warning_coins and wf.get("warning"):
                    return {"ok": False, "error": f"{market} 투자유의 종목 — 매수 차단 (Entry 설정에서 해제 가능)"}
                if self.config.block_caution_coins and wf.get("caution"):
                    return {"ok": False, "error": f"{market} 주의환기({','.join(wf.get('kinds', []))}) — 매수 차단"}
                if pct > 0:
                    free = float(self.client.get_balance(self._quote_currency))
                    amt = min(free * pct / 100.0, free * 0.9995)  # 수수료 여유(fee room)
                else:
                    amt = float(krw)
                min_krw = getattr(self.client, "MIN_ORDER_KRW", 5000.0)
                if amt < min_krw:
                    return {"ok": False, "error": f"매수 금액 ₩{amt:.0f} < 최소주문 ₩{min_krw:.0f}"}
                od = self.client.market_buy(market, amt)
                # ★ 수동 매수도 self.positions 에 등록 → 패널 표시(슬롯 수 무관). 봇 관리 여부는 토글.
                pos = self._register_manual_position(market, amt, od)
                managed = bool(self.config.manual_manage_enabled)
                logger.info("[SPOT_GAZUA] 🟢 퀵매수 %s ₩%.0f%s → 포지션 등록(%s)",
                            market, amt, f" ({pct:.0f}%)" if pct > 0 else "",
                            "봇 관리" if managed else "관망")
                return {"ok": True, "side": "buy", "market": market, "krw": round(amt, 0),
                        "managed": managed, "registered": bool(pos), "order": od}
            else:
                if pct > 0:
                    q = float(self.client.get_balance(base_currency(market))) * pct / 100.0
                else:
                    q = float(qty)
                    if q <= 0:
                        q = float(self.client.get_balance(base_currency(market)))
                if q <= 0:
                    return {"ok": False, "error": "매도할 보유 수량 없음"}
                od = self.client.market_sell(market, q)
                logger.info("[SPOT_GAZUA] 🔴 퀵매도 %s qty=%.8f%s", market, q, f" ({pct:.0f}%)" if pct > 0 else "")
                return {"ok": True, "side": "sell", "market": market, "qty": q, "order": od}
        except Exception as exc:
            logger.error("[SPOT_GAZUA] quick_order %s %s FAILED: %s", market, side, exc)
            return {"ok": False, "error": str(exc)}

    def _register_manual_position(self, market: str, krw_spend: float, order: Dict[str, Any]):
        """퀵트레이드 수동 매수 → self.positions 등록 (패널 표시·슬롯 수 무관).
        체결 평단/수량은 wait_order 로 전량 체결까지 확정(_execute_entry 동일 패턴).
        manual_manage_enabled=True → SL/TP 계산·서버 TP 박기(봇 자동 관리).
        False(관망) → SL/TP=0(표시 '—'), 봇 자동 청산 안 함 — 청산은 사람(청산 버튼)."""
        order = order if isinstance(order, dict) else {}
        order_uuid = str(order.get("uuid", "") or "")
        exec_qty = float(order.get("executed_volume", 0) or 0)
        fill_price = float(order.get("avg_price", 0) or 0)
        # 시장가 매수는 여러 호가 분할 체결 가능 → 전량 체결까지 대기 후 최종 평단/수량 확정.
        if order_uuid:
            try:
                od2 = self.client.wait_order(uuid=order_uuid, market=market, timeout_sec=10.0, poll_interval=0.5)
                exec_qty = float(od2.get("executed_volume", 0) or 0) or exec_qty
                fill_price = float(od2.get("avg_price", 0) or 0) or fill_price
            except Exception as q_exc:
                logger.warning("[SPOT_GAZUA] 퀵매수 wait_order reconcile %s 실패: %s", market, q_exc)
        price = fill_price or self._get_price(market)
        qty = exec_qty if exec_qty > 0 else (krw_spend / price if price > 0 else 0.0)
        if price <= 0 or qty <= 0:
            logger.warning("[SPOT_GAZUA] 수동 포지션 등록 skip %s (price=%.4f qty=%.8f)", market, price, qty)
            return None
        with self._lock:
            # 같은 마켓 기존 포지션 있으면 중복 행 방지(거래소 보유는 합산되나 패널 1행 유지).
            if any(p.market == market for p in self.positions):
                logger.info("[SPOT_GAZUA] %s 기존 포지션 존재 — 수동 매수 중복 등록 skip", market)
                return None
            managed = bool(self.config.manual_manage_enabled)
            if managed:
                atr = self._estimate_atr(market, price) or price * 0.02
                t = self._compute_targets(price, atr)
                sl, tp1, tp2, atr_used = t.sl, t.tp1, t.tp2, t.atr_used
            else:
                sl = tp1 = tp2 = atr_used = 0.0
            pos = SpotGazuaPosition(
                market=market, direction="LONG", entry_price=price, qty=qty,
                tp1=tp1, tp2=tp2, sl=sl, atr_used=atr_used,
                entry_ts=time.time(), trailing_high=price, krw_spent=krw_spend,
                paper=False, order_uuid=order_uuid, manual=True,
            )
            if managed:
                self._place_tp_orders(pos)   # 봇 관리: TP1/TP2 서버측 지정가 매도 박기
            self.positions.append(pos)
            self.state = FocusState.POSITIONED
            self._record_journal("ENTRY", pos, price,
                                 reason="수동 매수(봇 관리)" if managed else "수동 매수(관망)")
            self._save_state()
            return pos

    # ── UI / Router ─────────────────────────────────────────
    def update_config(self, config: Optional[Dict[str, Any]] = None, **kw) -> Dict[str, Any]:
        """dict(Bybit FOCUS 패턴) 또는 kwargs 둘 다 허용."""
        with self._lock:
            merged: Dict[str, Any] = dict(config) if isinstance(config, dict) else {}
            merged.update(kw)
            prev_paper = self.config.paper
            for k, v in merged.items():
                if not hasattr(self.config, k):
                    continue
                # ★ dataclass 현재값 타입 기준 강제변환 — query param(전부 문자열)·UI 입력도 안전.
                #   (672 미러 필드를 generic 라우터로 받으려면 필수: "17"→17, "true"→True 등)
                cur = getattr(self.config, k)
                try:
                    if isinstance(cur, bool):
                        v = v if isinstance(v, bool) else str(v).strip().lower() in ("true", "1", "yes", "on")
                    elif isinstance(cur, int):          # bool 은 위에서 이미 처리됨
                        v = int(float(v))
                    elif isinstance(cur, float):
                        v = float(v)
                    elif isinstance(cur, list):
                        v = v if isinstance(v, list) else [s.strip() for s in str(v).split(",") if s.strip()]
                    elif isinstance(cur, str):
                        v = str(v)
                except (ValueError, TypeError):
                    continue                            # 변환 불가 → 무시(기존값 유지)
                setattr(self.config, k, v)
            # ★ paper↔live 전환 시 모드 안 맞는 포지션 정리 (섞임 방지)
            if "paper" in merged and bool(merged["paper"]) != bool(prev_paper):
                if not self.config.paper:  # paper→live: 가상 포지션 버림(실제 없음)
                    removed = [p.market for p in self.positions if p.paper]
                    self.positions = [p for p in self.positions if not p.paper]
                    if removed:
                        logger.info("[SPOT_GAZUA] LIVE 전환 — 가상(paper) 포지션 정리: %s", removed)
                else:  # live→paper: 실거래 포지션은 봇이 계속 실관리(pos.paper 기준), 경고만
                    live = [p.market for p in self.positions if not p.paper]
                    if live:
                        logger.warning("[SPOT_GAZUA] PAPER 전환 — 실거래 포지션 %s 남음(봇 계속 실관리). Upbit 앱 확인", live)
            self._save_state()
            return asdict(self.config)

    def get_status(self, *, with_account: bool = True) -> Dict[str, Any]:
        poss = []
        upnl_krw = 0.0
        fee_r = max(0.0, float(getattr(self.config, "fee_rate_pct", 0.0))) / 100.0
        for p in self.positions:
            d = p.to_dict()
            cur = self._get_price(p.market)
            d["current_price"] = cur
            # net: 가격변동% − 왕복 수수료%(매수 fee_r + 매도 fee_r×ratio). 부모님 입력 fee_rate_pct 반영.
            if cur and p.entry_price > 0:
                ratio = cur / p.entry_price
                gross_pct = (ratio - 1.0) * 100.0
                fee_pct = (fee_r + fee_r * ratio) * 100.0
                d["pnl_pct"] = round(gross_pct - fee_pct, 2)
                upnl_krw += (cur - p.entry_price) * p.qty - (p.entry_price + cur) * p.qty * fee_r
            else:
                d["pnl_pct"] = 0.0
            # TP1까지 진행률(%) — 진입가→tp1 구간 (음수=평단 아래)
            if cur and p.tp1 and p.tp1 > p.entry_price:
                d["progress_pct"] = round((cur - p.entry_price) / (p.tp1 - p.entry_price) * 100, 1)
            else:
                d["progress_pct"] = 0.0
            d["hold_sec"] = round(time.time() - p.entry_ts, 1)
            poss.append(d)
        jr = self.journal_summary()
        out = {
            "enabled": self.config.enabled,
            "paper": self.config.paper,
            "state": self.state.value,
            "positions": poss,
            "unrealized_krw": round(upnl_krw, 0),
            "daily_plans_used": self.daily_plans_used,
            "daily_sl_count": self.daily_sl_count,
            "today_pnl_krw": jr["today_pnl_krw"],
            "total_pnl_krw": jr["total_pnl_krw"],
            # ★ [2026-06-21] GateLedger snapshot — "왜 침묵했나" 관제판(게이트별 pass/reject). ON 일 때만.
            "gate_stats": (self._gate_ledger.snapshot()
                           if (getattr(self, "_gate_ledger", None) is not None
                               and getattr(self.config, "gate_ledger_enabled", False))
                           else None),
            "config": asdict(self.config),
        }
        # ★ [2026-06-20] 수동(퀵트레이드) 진입 권장 예산 — 봇 슬롯 예산(_effective_budget) 그대로(총자산÷슬롯).
        #   UI가 매수금액 기본값으로 미리 채움(부모: "수동 진입 시 예산도 권장값으로"). 새 size 로직 신설 X.
        try:
            out["rec_budget"] = round(self._effective_budget())
        except Exception:
            out["rec_budget"] = 0
        if with_account and not self.config.paper:
            out["account"] = self.account_summary()
        return out


# ── Upbit 현물 = SpotGazuaManager 의 한 peer (Bithumb/Bybit/Binance 와 동격 자식) ──────────
#   기본값(UpbitTradeClient·runtime/upbit/·KRW)은 SpotGazuaManager 가 제공 → 얇은 상속.
class UpbitGazuaManager(SpotGazuaManager):
    """Upbit 현물 long-only FOCUS. SpotGazuaManager 본체 그대로 (기본 client/state 가 Upbit)."""
    pass
