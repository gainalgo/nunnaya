# ============================================================
# Binance 현물(spot) FOCUS 매니저 — SpotGazuaManager 상속
# ------------------------------------------------------------
# [2026-06-23 부모] "바이낸스도 하나 만들어볼까? 선물+현물" — 현물(USDT) 먼저.
# SpotGazuaManager 는 거래소 무관(client 인터페이스 + state_path 만 의존)이라
# Binance 현물 client + 별도 state_path 로 그대로 재사용한다. 두뇌(스캔/진입/청산/
# 점수/예산/존버) 로직 일절 복제 안 함 — client 만 BinanceSpotTradeClient.
# (Bybit 현물 BybitSpotGazuaManager 와 동일 구조 — quote=USDT 미러.)
#
# Upbit(KRW) ↔ Binance 현물(USDT) 차이:
#   - 심볼 'BTCUSDT'(KRW-BTC 아님) → _normalize_market override.
#   - market_warning(투자유의/주의환기) 없음 → client.get_market_warnings()={}.
#   - quote=USDT.
#
# 격리: state/journal 은 runtime/binance_spot/ 로 분리 → Upbit·Bybit·선물과 자본·상태 독립.
# ============================================================
from __future__ import annotations

import os
from typing import Any, Optional

from app.manager.spot_gazua_manager import SpotGazuaManager


class BinanceSpotGazuaManager(SpotGazuaManager):
    """Binance 현물(USDT) long-only FOCUS. SpotGazuaManager 와 동일 로직, client/state 만 Binance 현물."""

    _quote_currency = "USDT"   # ★견적통화 USDT (잔고/예산 조회 키). Upbit/Bithumb=KRW.

    def __init__(self, system: Any = None, client: Any = None, *, state_path: Optional[str] = None):
        if client is None:
            from app.integrations.binance_spot_trade import BinanceSpotTradeClient
            client = BinanceSpotTradeClient(
                os.getenv("BINANCE_API_KEY", ""), os.getenv("BINANCE_API_SECRET", "")
            )
        if state_path is None:
            try:
                from app.core.runtime_paths import RuntimePaths
                state_path = RuntimePaths(exchange="binance_spot").custom("binance_spot_focus_config.json")
            except Exception:
                state_path = os.path.join("runtime", "binance_spot", "binance_spot_focus_config.json")
                os.makedirs(os.path.dirname(state_path), exist_ok=True)
        # ★ [2026-06-23 부모 "임의 강제잠금 풀어라"] paper 는 부모 토글이 권위 — 자동 매부팅 강제 제거.
        #   신규 거래소 *첫 기동(config 파일 없음)* 에만 1회 paper=True 안전 기본값(검증 안 된 거래소 관측부터).
        #   이후엔 부모가 UI 로 paper on/off 자유(저장값 그대로 유지·재부팅에도 안 잠김).
        _fresh = not os.path.exists(state_path)
        super().__init__(system=system, client=client, state_path=state_path)
        # ★ [2026-06-23 감사 low] 첫 기동은 *무조건* paper (config.paper 기본값에 의존 안 함 — 향후
        #   default 가 바뀌어도 강제 무력화 안 되게). 파일 생긴 뒤엔 부모 토글이 권위(안 잠김).
        if _fresh and getattr(self.config, "paper", True) is not True:
            try:
                self.update_config(paper=True)
            except Exception:
                self.config.paper = True
            import logging as _lg
            _lg.getLogger(__name__).info("[binance_spot] 첫 기동 paper 기본값(이후 부모 토글 자유)")
        # 저널도 binance_spot 디렉터리·이름으로 (자본/기록 격리)
        self.journal_path = os.path.join(os.path.dirname(state_path), "binance_spot_focus_journal.jsonl")

    def _normalize_market(self, market: str) -> str:
        """수동입력 마켓 정규화 — Binance 현물: 'BTC'/'KRW-BTC'/'btcusdt' → 'BTCUSDT'."""
        m = str(market).upper().strip().replace("/", "")
        if m.startswith("KRW-"):
            m = m[4:]
        m = m.replace("-", "")
        if not m.endswith("USDT"):
            m = f"{m}USDT"
        return m
