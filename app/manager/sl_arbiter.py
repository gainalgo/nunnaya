# ============================================================
# SLArbiter — SL/청산 의견 중재자 (master §4.2 게이트, A)
# ------------------------------------------------------------
# 한 포지션의 SL/청산에 대해 여러 부품(cycle_tp·longhold·trailing·triage)이
# 동시에 다른 의견을 낼 때, 단 하나의 결정을 내린다.
#   부품 = 제안(SLProposal)만 / 실행 = 호출자(매니저) / 판단 = 여기.
#
# ★ 순수 함수: arbitrate()는 입력만으로 결정 — I/O·전역상태·부수효과 없음.
#   → 단위테스트 100%, 미배선 동안 라이브 영향 0. (DESIGN_A_sl_arbiter_20260617.md)
#
# 핵심 규칙 (Fable5 §4.2 = INV-3 코드강제):
#   - has_liquidation 비대칭: 선물(True)=fast_cut 생명 / 현물(False)=존버 허용
#   - 존버(FREEZE) 살아있는 동안 trailing TIGHTEN 기각 (존버 코인 컷 사고 차단)
# ============================================================
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List

logger = logging.getLogger(__name__)

# 액션 상수 (제안/결정 공용)
EXIT_NOW = "EXIT_NOW"   # 지금 매도 (SL 도달 등)
FREEZE = "FREEZE"       # 매도 금지·존버 (longhold)
TIGHTEN = "TIGHTEN"     # SL 위로 이동 (trailing)
HOLD = "HOLD"           # 아무것도 안 함 (유지)


@dataclass
class SLProposal:
    """부품 → 중재자. 한 부품의 의견 1건."""
    source: str                 # "cycle_tp" | "longhold" | "trailing" | "triage" | "manual"
    action: str                 # EXIT_NOW | FREEZE | TIGHTEN | HOLD
    new_sl: float = 0.0         # TIGHTEN 시 제안 SL 가격
    reason: str = ""


@dataclass
class SLDecision:
    """중재자 → 매니저. 최종 결정 1건."""
    action: str                 # EXIT_NOW | HOLD | TIGHTEN
    new_sl: float = 0.0
    reason: str = ""
    source: str = ""            # 채택된 제안 출처


class SLArbiter:
    """SL/청산 의견 중재 — 순수. 부수효과 없음."""

    def arbitrate(self, *, has_liquidation: bool, proposals: List[SLProposal],
                  freeze_active: bool, current_sl: float = 0.0) -> SLDecision:
        """제안들을 받아 단 하나의 결정. (DESIGN_A §A.3 규칙 순서)

        Args:
            has_liquidation: 거래소 청산 유무 (True=선물 fast_cut / False=현물 존버허용).
            proposals: 이번 tick 부품들의 제안.
            freeze_active: 현물 존버(longhold) 자격이 살아있는가 (BTC양호+등록).
                           BTC 하락장이면 호출자가 False 로 넣음(longhold 헬퍼 내부판정).
            current_sl: 현재 SL 가격 — TIGHTEN 이 아래로 못 내리게 가드.
        """
        props = list(proposals or [])
        has_exit = any(p.action == EXIT_NOW for p in props)
        tighten = [p for p in props if p.action == TIGHTEN]

        # 1) EXIT_NOW 우선권 — 거래소 분기 (INV-3 핵심)
        if has_exit:
            exit_p = next(p for p in props if p.action == EXIT_NOW)
            if has_liquidation:
                # 선물: SL 도달 = 무조건 매도. FREEZE 무시 (청산 방어=생명).
                return SLDecision(action=EXIT_NOW, reason=exit_p.reason or "exit:futures_sl",
                                  source=exit_p.source)
            # 현물: 유효한 FREEZE 있으면 ↓ 규칙2로 (존버). 없으면 매도.
            if not freeze_active:
                # BTC bear 등 → 정상 SL 매도 (떨어지는 칼 존버 방지)
                return SLDecision(action=EXIT_NOW, reason=exit_p.reason or "exit:spot_no_freeze",
                                  source=exit_p.source)
            # freeze_active=True → 규칙2(HOLD)로 떨어짐

        # 2) FREEZE 게이트 (현물만) — 존버 살아있으면 매도 안 함
        if freeze_active and not has_liquidation:
            # 3) 존버 보호: 이 동안 TIGHTEN 은 기각 (존버 코인을 trailing 이 컷 못하게)
            if tighten:
                logger.debug("[SLArbiter] 존버 중 TIGHTEN %d건 기각", len(tighten))
            return SLDecision(action=HOLD, reason="freeze:longhold_active", source="longhold")

        # 4) TIGHTEN 합의 — SL 은 위로만 (가장 높은 new_sl, current_sl 보다 높을 때만)
        if tighten:
            best = max(tighten, key=lambda p: p.new_sl)
            if best.new_sl > current_sl:
                return SLDecision(action=TIGHTEN, new_sl=best.new_sl,
                                  reason=best.reason or "trailing:tighten", source=best.source)
            # 제안 SL 이 현재 이하 → 무시 (역행 방지)

        # 5) 기본
        return SLDecision(action=HOLD, reason="no_actionable_proposal", source="")


# 모듈 단일 인스턴스 (순수라 상태 없음 — 공유 안전)
_arbiter = SLArbiter()


def arbitrate(*, has_liquidation: bool, proposals: List[SLProposal],
              freeze_active: bool, current_sl: float = 0.0) -> SLDecision:
    """모듈 레벨 헬퍼 — 공용 인스턴스로 중재."""
    return _arbiter.arbitrate(
        has_liquidation=has_liquidation, proposals=proposals,
        freeze_active=freeze_active, current_sl=current_sl,
    )
