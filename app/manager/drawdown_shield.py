"""Drawdown Shield -- equity high watermark 추적 + 연패 시 자동 리스크 축소.

형 server에서 동생 server로 보내는 선물.
FOCUS 전략의 급격한 드로다운(e.g. equity peak 대비 10% 증발)을 감지하고,
깊어질수록 conviction penalty를 높여 Scanner가 자연스럽게 선별적이 되도록 유도.

직접 트레이드를 차단하지 않음 — advisory 정보만 제공.
focus_manager에서 get_protection_level()로 penalty를 받아 conviction에 반영.

[2026-04-18 리팩토링 — 동생 서버]
  기존: 누적 PnL 기준 DD% = (peak_pnl - current_pnl) / peak_pnl * 100
        → current_pnl이 음수로 떨어지면 DD%가 100% 초과로 발산 (213% 같은 이상값)
  신규: Equity(USDT) 기준 DD% = (peak_equity - current_equity) / peak_equity * 100
        → equity는 레버리지 perpetual에서 항상 > 0 이므로 자연스럽게 0~100% 범위
        → peak는 세션 간 유지 (high watermark), current_equity < peak_equity 일 때만 DD
        → 신규 고점 찍으면 DD 자동 0

파일:
  - runtime/drawdown_shield.json — 워터마크 + 히스토리 상태
"""
from __future__ import annotations

import datetime as _dt
import logging
import os
import time
from typing import Any, Dict, List

from app.core.io_utils import safe_load_json, safe_write_json

logger = logging.getLogger(__name__)

STATE_PATH = os.path.join("runtime", "drawdown_shield.json")

# ── 누적 드로다운 구간 (peak equity 대비 %) ──
# [2026-05-17 100점 ×10] conviction_penalty 옛 -3/-2/-1 → -30/-20/-10
_CUMUL_TIERS = [
    # (threshold_pct, level, label, conviction_penalty)
    (20.0, 3, "CRISIS",  -30),
    (10.0, 2, "DEFEND",  -20),
    (5.0,  1, "CAUTION", -10),
    (0.0,  0, "NORMAL",    0),
]

# ── 일간 드로다운 구간 (오늘 peak PnL 대비 절대금액) ──
# [2026-05-17 100점 ×10]
_DAILY_TIERS = [
    # (threshold_usd, level, label, conviction_penalty)
    (100.0, 3, "CRISIS",  -30),
    (60.0,  2, "DEFEND",  -20),
    (30.0,  1, "CAUTION", -10),
    (0.0,   0, "NORMAL",    0),
]

# ── 💰 [2026-06-12 부모 긴급] 자본 유출입 감지 임계 ──
#   한 tick 사이 equity 변화가 이 비율(12%) 이상이면 거래손익 아닌 입출금으로 간주 → peak 재조정.
#   근거: 포지션=자본 일부(슬롯 20%)·SL 수% → 단일 tick 거래손익은 자본의 수% 이내. 12%+ = 입출금.
_CAPITAL_FLOW_STEP_PCT = 0.12

# ── 한국어 메시지 템플릿 ──
_MESSAGES = {
    0: "정상 -- 드로다운 없음",
    1: "주의 -- 최고점 대비 {pct:.1f}% 하락 (${amt:.1f})",
    2: "방어 -- 최고점 대비 {pct:.1f}% 하락, 신규 진입 자제",
    3: "위기 -- 최고점 대비 {pct:.1f}% 하락, 진입 중단 권고",
}


class DrawdownShield:
    """Equity high watermark 기반 드로다운 방어 시스템."""

    def __init__(self):
        self._state = self._load_state()
        logger.info(
            "[DrawdownShield] 초기화 — peak_equity=$%.2f, current=$%.2f, dd=%.2f%% (max=%.2f%%)",
            self._state.get("peak_equity", 0.0),
            self._state.get("current_equity", 0.0),
            self._state.get("drawdown_pct", 0.0),
            self._state.get("max_drawdown_pct", 0.0),
        )

    # ================================================================
    #  Public API
    # ================================================================

    def update(self, current_equity: float, realized_pnl_today: float) -> Dict[str, Any]:
        """매 tick (또는 주기적) 호출 — 워터마크 갱신 + 현재 보호 상태 반환.

        Args:
            current_equity: 현재 계정 equity (USDT) — cash + 미실현 PnL 포함, 항상 > 0
            realized_pnl_today: 오늘(07:00 KST~) 실현 PnL — 일간 드로다운 계산용

        Warmup:
            current_equity가 0 이하이면 reconcile 미완료로 간주하여 업데이트 스킵.
        """
        s = self._state
        now = time.time()

        # ── Warmup 가드: reconcile 미완료 시 스킵 ──
        try:
            eq = float(current_equity)
        except (TypeError, ValueError):
            eq = 0.0
        if eq <= 0.0:
            logger.debug("[DrawdownShield] warmup — equity=%.2f 무효, 업데이트 스킵", eq)
            return self.get_protection_level()

        # ── 💰 [2026-06-12 부모 긴급] 자본 유출입(입출금) 감지 → 워터마크 재조정 ──
        #   버그: 출금하면 equity 떨어지는데 peak 는 그대로 → 가짜 DD → CRISIS → 봇 멈춤. 입출금은 손익 아님.
        #   한 tick 사이 큰 equity 점프(≥12%)는 거래손익으론 불가능(포지션=자본 일부·SL 수%) → 입출금으로 간주,
        #   peak 를 Δ만큼 같이 이동(출금=peak↓/입금=peak↑). 거래로 인한 점진 변동엔 미발동.
        _last_eq = s.get("last_equity_for_flow", 0.0)
        if _last_eq > 0 and s["peak_equity"] > 0:
            _step = eq - _last_eq
            if abs(_step) >= _last_eq * _CAPITAL_FLOW_STEP_PCT:
                _old_peak = s["peak_equity"]
                # 출금: peak+step(=peak−출금액). 입금: peak+step(상향). 단 peak<eq 면 eq 로 클램프(DD 0).
                s["peak_equity"] = round(max(eq, s["peak_equity"] + _step), 4)
                logger.warning(
                    "[DrawdownShield] 💰 자본 유출입 감지 ($%.2f→$%.2f Δ$%+.2f) → peak $%.2f→$%.2f 재조정 "
                    "(입출금=손익 아님, 가짜 DD 방지)",
                    _last_eq, eq, _step, _old_peak, s["peak_equity"],
                )
        s["last_equity_for_flow"] = round(eq, 4)

        # ── 누적 equity 워터마크 갱신 ──
        if eq > s["peak_equity"]:
            s["peak_equity"] = round(eq, 4)
            s["peak_equity_ts"] = now

        s["current_equity"] = round(eq, 4)

        # 누적 드로다운 계산 — equity 기준이므로 자연스럽게 0~100%
        dd_amt = max(s["peak_equity"] - eq, 0.0)
        dd_pct = (dd_amt / s["peak_equity"] * 100.0) if s["peak_equity"] > 0 else 0.0
        # 안전장치: 수치 이상 시 cap (peak > 0이면 이론상 0~100이지만 방어적)
        if dd_pct > 100.0:
            dd_pct = 100.0

        s["drawdown_amount"] = round(dd_amt, 4)
        s["drawdown_pct"] = round(dd_pct, 2)

        # 최악 기록 갱신
        if dd_amt > s["max_drawdown_amount"]:
            s["max_drawdown_amount"] = round(dd_amt, 4)
        if dd_pct > s["max_drawdown_pct"]:
            s["max_drawdown_pct"] = round(dd_pct, 2)

        # ── 일간 워터마크 갱신 (realized PnL 금액 기준 — 기존 유지) ──
        try:
            rp = float(realized_pnl_today)
        except (TypeError, ValueError):
            rp = 0.0
        if rp > s["daily_peak_pnl"]:
            s["daily_peak_pnl"] = round(rp, 4)

        s["daily_current_pnl"] = round(rp, 4)
        s["daily_drawdown"] = round(max(s["daily_peak_pnl"] - rp, 0.0), 4)

        s["last_update_ts"] = now

        # 상태 저장
        self._save_state()

        return self.get_protection_level()

    def get_protection_level(self) -> Dict[str, Any]:
        """현재 드로다운 깊이에 따른 보호 레벨 반환.

        누적 레벨과 일간 레벨 중 더 나쁜 쪽을 채택.
        """
        s = self._state

        # 누적 레벨 (equity 기준)
        cumul_level, cumul_label, cumul_penalty = self._resolve_cumul_tier(s["drawdown_pct"])

        # 일간 레벨 (realized PnL 금액 기준)
        daily_level, daily_label, daily_penalty = self._resolve_daily_tier(s["daily_drawdown"])

        # 더 나쁜 쪽 채택
        if daily_level > cumul_level:
            level = daily_level
            label = daily_label
            penalty = daily_penalty
            source = "daily"
        else:
            level = cumul_level
            label = cumul_label
            penalty = cumul_penalty
            source = "cumulative"

        dd_pct = s["drawdown_pct"]
        dd_amt = s["drawdown_amount"]
        msg = _MESSAGES.get(level, "").format(pct=dd_pct, amt=dd_amt)

        return {
            "protection_level": level,
            "protection_label": label,
            "conviction_penalty": penalty,
            "message": msg,
            "source": source,
            "cumulative": {"level": cumul_level, "label": cumul_label, "penalty": cumul_penalty},
            "daily": {"level": daily_level, "label": daily_label, "penalty": daily_penalty},
        }

    def get_status(self) -> Dict[str, Any]:
        """API/대시보드 표시용 전체 상태."""
        s = self._state
        prot = self.get_protection_level()

        return {
            "enabled": True,
            # 신규 equity 기반 키
            "peak_equity": s["peak_equity"],
            "current_equity": s["current_equity"],
            "drawdown_amount": s["drawdown_amount"],
            "drawdown_pct": s["drawdown_pct"],
            "max_drawdown_amount": s["max_drawdown_amount"],
            "max_drawdown_pct": s["max_drawdown_pct"],
            "peak_equity_ts": s.get("peak_equity_ts", 0.0),
            # 일간 (기존 유지)
            "daily_peak_pnl": s["daily_peak_pnl"],
            "daily_current_pnl": s["daily_current_pnl"],
            "daily_drawdown": s["daily_drawdown"],
            # 보호 레벨
            "protection_level": prot["protection_level"],
            "protection_label": prot["protection_label"],
            "conviction_penalty": prot["conviction_penalty"],
            "message": prot["message"],
            "last_update_ts": s["last_update_ts"],
        }

    def reset_daily(self) -> None:
        """07:00 KST 리셋 시 호출 — 전일 일간 드로다운 기록 저장 후 초기화."""
        s = self._state

        # 전일 기록 히스토리에 추가
        today_label = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")
        record = {
            "date": today_label,
            "daily_peak_pnl": s["daily_peak_pnl"],
            "daily_current_pnl": s["daily_current_pnl"],
            "daily_drawdown": s["daily_drawdown"],
            "peak_equity": s["peak_equity"],
            "current_equity": s["current_equity"],
            "drawdown_pct": s["drawdown_pct"],
            "max_drawdown_pct": s["max_drawdown_pct"],
            "ts": time.time(),
        }
        s.setdefault("history", []).append(record)

        # 히스토리 90일 유지
        if len(s["history"]) > 90:
            s["history"] = s["history"][-90:]

        logger.info(
            "[DrawdownShield] 일간 리셋 — 전일 peak=$%.2f, dd=$%.2f",
            s["daily_peak_pnl"], s["daily_drawdown"],
        )

        # 일간 수치 초기화
        s["daily_peak_pnl"] = 0.0
        s["daily_current_pnl"] = 0.0
        s["daily_drawdown"] = 0.0

        self._save_state()

    def reset_cumulative(self) -> None:
        """관리자용: 누적 워터마크 리셋.

        장기간 엔진 정지/재시작 후 혹은 자본 변동(입금/출금) 시 수동 호출.
        max_drawdown_pct/amount 까지 함께 초기화하여 깨끗한 재출발.
        """
        s = self._state
        old_peak = s["peak_equity"]
        old_dd = s["drawdown_pct"]
        s["peak_equity"] = 0.0
        s["current_equity"] = 0.0
        s["drawdown_amount"] = 0.0
        s["drawdown_pct"] = 0.0
        s["max_drawdown_amount"] = 0.0
        s["max_drawdown_pct"] = 0.0
        s["peak_equity_ts"] = 0.0
        logger.info(
            "[DrawdownShield] 누적 리셋 — 이전 peak=$%.2f, dd=%.2f%% → 0으로 초기화",
            old_peak, old_dd,
        )
        self._save_state()

    def get_history(self) -> List[Dict]:
        """일간 드로다운 히스토리 반환 (최신순)."""
        history = list(self._state.get("history", []))
        history.reverse()
        return history

    # ================================================================
    #  Persistence
    # ================================================================

    def _save_state(self) -> None:
        """runtime/drawdown_shield.json 에 상태 저장."""
        try:
            safe_write_json(STATE_PATH, self._state)
        except Exception as exc:
            logger.error("[DrawdownShield] 상태 저장 실패: %s", exc)

    def _load_state(self) -> Dict[str, Any]:
        """파일에서 상태 로드. 없거나 깨졌으면 기본값.

        [2026-04-18] 구 버전(peak_equity_pnl 기반) 발견 시 자동 마이그레이션:
          - 옛 PnL 기반 메트릭은 폐기 (의미 다름)
          - 새 peak_equity 0.0으로 시작 → 첫 update()에서 현재 equity가 peak가 됨
          - history는 보존
        """
        defaults = {
            # 신규 equity 기반 키
            "peak_equity": 0.0,
            "current_equity": 0.0,
            "drawdown_amount": 0.0,
            "drawdown_pct": 0.0,
            "max_drawdown_amount": 0.0,
            "max_drawdown_pct": 0.0,
            "peak_equity_ts": 0.0,
            # 일간 (기존 유지)
            "daily_peak_pnl": 0.0,
            "daily_current_pnl": 0.0,
            "daily_drawdown": 0.0,
            # 메타
            "last_update_ts": 0.0,
            "history": [],
        }

        loaded = safe_load_json(STATE_PATH, default=None)
        if loaded is None or not isinstance(loaded, dict):
            logger.info("[DrawdownShield] 상태 파일 없음/손상 — 기본값으로 초기화")
            return defaults

        # ── 마이그레이션: 구 버전 키(peak_equity_pnl) 발견 시 ──
        if "peak_equity_pnl" in loaded and "peak_equity" not in loaded:
            logger.warning(
                "[DrawdownShield] 구 버전 상태 파일 감지 (peak_equity_pnl=$%.2f, dd=%.2f%%) "
                "— PnL 기반 → Equity 기반 리팩토링 마이그레이션. 누적 메트릭 리셋.",
                float(loaded.get("peak_equity_pnl", 0.0)),
                float(loaded.get("drawdown_pct", 0.0)),
            )
            # 옛 PnL 기반 누적 지표 전부 폐기, 새 스키마로 초기화
            migrated = dict(defaults)
            # 일간 + history 는 이어받음
            for k in ("daily_peak_pnl", "daily_current_pnl", "daily_drawdown",
                      "last_update_ts", "history"):
                if k in loaded:
                    migrated[k] = loaded[k]
            return self._coerce_types(migrated, defaults)

        # 누락 필드 보정 (버전업 대응)
        for key, val in defaults.items():
            if key not in loaded:
                loaded[key] = val

        return self._coerce_types(loaded, defaults)

    @staticmethod
    def _coerce_types(data: Dict[str, Any], defaults: Dict[str, Any]) -> Dict[str, Any]:
        """타입 보정 — 문자열이 들어왔을 경우 방어."""
        for key in defaults:
            if key == "history":
                if not isinstance(data.get(key), list):
                    data[key] = []
                else:
                    # ★ Phase H (2026-04-20 형 letter#3 A-10): history 내부 dict recursive type check
                    _clean_history = []
                    for _h in data[key]:
                        if isinstance(_h, dict):
                            _clean_history.append(_h)
                    data[key] = _clean_history
            else:
                try:
                    data[key] = float(data.get(key, defaults[key]))
                except (TypeError, ValueError):
                    data[key] = defaults[key]
        return data

    # ================================================================
    #  Internal
    # ================================================================

    def set_tiers(self, cumul=None, daily=None):
        """[2026-06-09 부모] focus_manager 가 config 값으로 티어 주입 (조정 여지).
        하드코딩 _CUMUL_TIERS/_DAILY_TIERS 를 런타임 config 로 덮어씀 (None 이면 기존 유지).
        각 티어 = (threshold, level, label, penalty) 리스트, threshold 내림차순."""
        if cumul:
            self._cumul_tiers = list(cumul)
        if daily:
            self._daily_tiers = list(daily)

    def _resolve_cumul_tier(self, dd_pct: float):
        """누적 드로다운 %에 대한 (level, label, penalty) 반환 (config 주입 티어 우선)."""
        for threshold, level, label, penalty in getattr(self, "_cumul_tiers", _CUMUL_TIERS):
            if dd_pct >= threshold:
                return level, label, penalty
        return 0, "NORMAL", 0

    def _resolve_daily_tier(self, dd_usd: float):
        """일간 드로다운 $에 대한 (level, label, penalty) 반환 (config 주입 티어 우선)."""
        for threshold, level, label, penalty in getattr(self, "_daily_tiers", _DAILY_TIERS):
            if dd_usd >= threshold:
                return level, label, penalty
        return 0, "NORMAL", 0


# ── Singleton ──
drawdown_shield = DrawdownShield()
