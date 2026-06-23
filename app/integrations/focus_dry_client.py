"""FOCUS 전용 dry-run wrapper — perpetual 가상 거래.

[2026-05-25 부모] paper 검증용. FOCUS 는 perpetual(LONG/SHORT/leverage) 인데
PaperTradeClient 는 spot 모델이라 안 맞음 → FOCUS 전용 dry wrapper 신설.

설계:
  - 시세 (get_kline, _linear_last_price): 실제 Bybit 위임 → 정확한 시장 데이터
  - 주문 (place_order, set_trading_stop): 가상 → 실제 거래소에 *절대* 안 보냄
  - 잔고 (get_balance): 가상 (virtual_usdt 고정)
  - 거래소 포지션 (get_positions): 빈 list (FOCUS self.positions 가 진실)
  - 나머지 메서드: __getattr__ 로 실제 client 위임

FOCUS 의 포지션 추적/PnL 계산은 self.positions + 실제 시세로 자체 수행되므로,
주문만 가상 처리하면 진입/청산 흐름 + 변동성 게이트가 그대로 검증됨.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


class FocusDryClient:
    """실제 Bybit client 를 감싸 주문만 가상 처리하는 dry-run wrapper."""

    def __init__(self, real_client: Any, virtual_usdt: float = 1000.0):
        # __dict__ 직접 설정 (─ __getattr__ 무한루프 방지)
        object.__setattr__(self, "_real", real_client)
        object.__setattr__(self, "_virtual_usdt", float(virtual_usdt))
        logger.info("[FOCUS-DRY] 🧪 Dry-run client 활성 — 시세=실제 / 주문=가상 / 잔고=$%.2f",
                    float(virtual_usdt))

    # ── 시세: 실제 Bybit 위임 (정확한 데이터 필수) ──
    def get_kline(self, *args, **kwargs):
        return self._real.get_kline(*args, **kwargs)

    def _linear_last_price(self, *args, **kwargs):
        return self._real._linear_last_price(*args, **kwargs)

    # ── 잔고: 가상 고정 ──
    def get_balance(self, currency: str, *, include_locked: bool = False) -> float:
        return self._virtual_usdt

    # ── 주문: 가상 (실거래 절대 차단) ──
    def place_order(self, *args, **kwargs) -> Dict[str, Any]:
        oid = f"DRY-{uuid.uuid4().hex[:12]}"
        _mkt = kwargs.get("market") or (args[0] if args else "?")
        _side = kwargs.get("side", "")
        _qty = kwargs.get("volume", "")
        logger.info("[FOCUS-DRY] 🧪 place_order 가상 (실거래 X): %s %s qty=%s", _mkt, _side, _qty)
        return {"ok": True, "orderId": oid, "result": {"orderId": oid}, "_dry": True}

    def set_trading_stop(self, *args, **kwargs) -> Dict[str, Any]:
        logger.debug("[FOCUS-DRY] 🧪 set_trading_stop 가상 (실거래 X)")
        return {"ok": True, "_dry": True}

    def cancel_order(self, *args, **kwargs) -> Dict[str, Any]:
        logger.debug("[FOCUS-DRY] 🧪 cancel_order 가상")
        return {"ok": True, "_dry": True}

    def set_leverage(self, *args, **kwargs) -> Dict[str, Any]:
        # 가상 — 실제 거래소 leverage 설정 안 함
        return {"ok": True, "_dry": True}

    def switch_position_mode(self, *args, **kwargs) -> Dict[str, Any]:
        # ★ [2026-06-23 감사] paper 누수 차단 — override 없으면 __getattr__ 로 실계좌
        #   account-wide 포지션모드(dualSidePosition) 변경 API 가 나감(진입 직전 호출됨).
        return {"ok": True, "_dry": True}

    # ── 거래소 포지션: 빈 list (FOCUS self.positions 가 진실) ──
    def get_positions(self, *args, **kwargs) -> List[Dict[str, Any]]:
        return []

    # ── [2026-06-23 감사] paper 누수 차단 — 아래 둘이 override 안 돼 __getattr__ 로
    #   실 거래소 인증 API(positionRisk·account)가 나가던 비대칭 누수. 명시 가상값으로 봉인.
    def list_open_positions(self, *args, **kwargs) -> List[Dict[str, Any]]:
        return []  # paper = 실계좌 포지션 조회 금지 (self.positions 가 진실)

    def get_available_margin(self, *args, **kwargs) -> float:
        return self._virtual_usdt  # paper = 가상 잔고 (실 account 조회 금지)

    # ── 나머지: 실제 client 위임 ──
    def __getattr__(self, name: str):
        # _real / _virtual_usdt 는 __dict__ 에 있어 여기 안 옴.
        # 그 외 못 찾은 속성/메서드는 실제 client 로 위임.
        if name in ("_real", "_virtual_usdt"):
            raise AttributeError(name)
        return getattr(self._real, name)
