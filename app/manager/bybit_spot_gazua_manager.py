# ============================================================
# Bybit 현물(spot) FOCUS 매니저 — SpotGazuaManager 상속
# ------------------------------------------------------------
# [2026-06-17 부모] "bybit에 현물부분부터 만들자" — USDT-현물 FOCUS 기준판.
# SpotGazuaManager 는 거래소 무관(client 인터페이스 + state_path 만 의존)이라
# Bybit 현물 client + 별도 state_path 로 그대로 재사용한다. 두뇌(스캔/진입/청산/
# 점수/예산/존버) 로직 일절 복제 안 함 — client 만 BybitSpotTradeClient.
#
# Upbit(KRW) ↔ Bybit 현물(USDT) 차이:
#   - 심볼 'BTCUSDT'(KRW-BTC 아님) → _normalize_market override.
#     ※ base_currency("BTCUSDT")=="BTC" 는 이미 동작(상위 매니저 호환).
#   - market_warning(투자유의/주의환기) 없음 → client.get_market_warnings()={}.
#   - quote=USDT. fee_rate_pct 등 USDT 튜닝은 대시보드/런타임에서 조정(여기선 기본 유지).
#
# 격리: state/journal 은 runtime/bybit_spot/ 로 분리 → Upbit·선물과 자본·상태 독립.
#   ※ 자본 cap: Bybit 통합계좌(UTA) USDT 는 선물과 공유 → budget(=USDT) 로 현물 몫 제한.
# ============================================================
from __future__ import annotations

import os
from typing import Any, Optional

from app.manager.spot_gazua_manager import SpotGazuaManager


class BybitSpotGazuaManager(SpotGazuaManager):
    """Bybit 현물(USDT) long-only FOCUS. SpotGazuaManager 와 동일 로직, client/state 만 Bybit 현물."""

    _quote_currency = "USDT"   # ★견적통화 USDT (잔고/예산 조회 키). Upbit/Bithumb=KRW.

    def __init__(self, system: Any = None, client: Any = None, *, state_path: Optional[str] = None):
        if client is None:
            from app.integrations.bybit_spot_trade import BybitSpotTradeClient
            # ★지갑 분리(서브계좌): BYBIT_SPOT_API_KEY/SECRET 쌍이 모두 있으면 그걸 사용.
            #   미설정이면 메인 BYBIT_API_KEY 로 fallback(=기존 동작, 호환 유지).
            #   [2026-06-19 부모] Bybit Unified 계좌서 현물 보유가 선물 reconcile 에
            #   orphan 으로 오인되던 문제(hs_mixin_reconcile) → 현물을 서브계좌로 분리해 지갑 격리.
            _spot_key = os.getenv("BYBIT_SPOT_API_KEY", "").strip()
            _spot_sec = os.getenv("BYBIT_SPOT_API_SECRET", "").strip()
            if _spot_key and _spot_sec:
                _key, _sec, _acct = _spot_key, _spot_sec, "SUB(BYBIT_SPOT_*)"
            else:
                _key = os.getenv("BYBIT_API_KEY", "")
                _sec = os.getenv("BYBIT_API_SECRET", "")
                _acct = "MAIN(BYBIT_*) — fallback(지갑 미분리)"
            try:
                import logging
                logging.getLogger(__name__).info("[bybit_spot] 거래 계좌 = %s", _acct)
            except Exception:
                pass
            client = BybitSpotTradeClient(_key, _sec)
        if state_path is None:
            try:
                from app.core.runtime_paths import RuntimePaths
                state_path = RuntimePaths(exchange="bybit_spot").custom("bybit_spot_focus_config.json")
            except Exception:
                state_path = os.path.join("runtime", "bybit_spot", "bybit_spot_focus_config.json")
                os.makedirs(os.path.dirname(state_path), exist_ok=True)
        super().__init__(system=system, client=client, state_path=state_path)
        # 저널도 bybit_spot 디렉터리·이름으로 (자본/기록 격리)
        self.journal_path = os.path.join(os.path.dirname(state_path), "bybit_spot_focus_journal.jsonl")

    def _normalize_market(self, market: str) -> str:
        """수동입력 마켓 정규화 — Bybit 현물: 'BTC'/'KRW-BTC'/'btcusdt' → 'BTCUSDT'."""
        m = str(market).upper().strip().replace("/", "")
        if m.startswith("KRW-"):
            m = m[4:]
        m = m.replace("-", "")
        if not m.endswith("USDT"):
            m = f"{m}USDT"
        return m
