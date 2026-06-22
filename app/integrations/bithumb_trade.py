# ============================================================
# Bithumb 현물 거래 클라이언트 — UpbitTradeClient 상속
# ------------------------------------------------------------
# [2026-06-17 부모] 빗썸 계정으로 같은 봇 한 벌 더. 빗썸 2.0 API 가
# Upbit 와 거의 100% 호환임을 실측 확인:
#   - 공개 /v1/market/all·candles/minutes·ticker·orderbook = Upbit 동일 포맷
#     (마켓코드 "KRW-BTC", market_warning 필드까지 동일)
#   - private = JWT(Bearer) + /v1/orders·/v1/accounts = Upbit 동일 구조
#     단 빗썸 JWT payload 에 timestamp 추가 요구.
# → base URL + _make_jwt(timestamp) 만 오버라이드. 나머지 ~20개 메서드
#   (get_kline/get_tickers/get_orderbook/market_buy/sell/get_balance/
#    accounts/place_order/get_order/cancel_order/wait_order/get_market_warnings)
#   전부 상속 재사용. 두뇌(manager/전략/점수/UI)는 client 만 갈아끼우면 그대로.
#
# ⚠️ 키 발급 후 검증할 것 (private 은 실측 못 함):
#   - /v1/orders 파라미터·응답이 Upbit 과 정확히 같은지 (market/side/ord_type/volume/price)
#   - /v1/accounts 잔고 응답 필드 (currency/balance/locked/avg_buy_price)
#   - MIN_ORDER_KRW (빗썸 최소주문 — 일단 Upbit 과 동일 5000 가정, 다르면 조정)
# ============================================================
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
import uuid as _uuid
from typing import Any, Dict, Optional
from urllib.parse import urlencode

from app.integrations.upbit_trade import (
    UpbitTradeClient, UpbitAPIError, _b64url,
)

logger = logging.getLogger(__name__)

BITHUMB_API_BASE = "https://api.bithumb.com"


class BithumbAPIError(UpbitAPIError):
    """빗썸 API 에러 (UpbitAPIError 와 동일 시그니처 — 두뇌 코드 except 호환)."""


class BithumbTradeClient(UpbitTradeClient):
    """빗썸 2.0 현물 클라이언트. Upbit 과 /v1/ 호환이라 거의 그대로 상속.
    base URL + JWT(timestamp 추가) 만 다름. 마켓코드/캔들/호가/주문 포맷 동일."""

    API_BASE = BITHUMB_API_BASE
    # ★ 빗썸 2.0 POST(/v1/orders 등)는 본문을 JSON 으로 보내야 함 (Upbit=form).
    #   form 본문 시 401 invalid_query_payload — query_hash(urlencode 기준)는 동일, 본문만 다름.
    _post_as_json = True

    def _make_jwt(self, query: Optional[Dict[str, Any]] = None) -> str:
        """빗썸 JWT = Upbit 와 동일(access_key·nonce·query_hash SHA512) + timestamp 추가."""
        if not self.access_key or not self.secret_key:
            raise BithumbAPIError("Bithumb API key/secret not configured")
        payload: Dict[str, Any] = {
            "access_key": self.access_key,
            "nonce": str(_uuid.uuid4()),
            "timestamp": int(time.time() * 1000),   # ★ 빗썸 요구(Upbit 와 유일한 차이)
        }
        if query:
            query_string = urlencode(query)
            payload["query_hash"] = hashlib.sha512(query_string.encode("utf-8")).hexdigest()
            payload["query_hash_alg"] = "SHA512"
        header = {"alg": "HS256", "typ": "JWT"}
        signing_input = (
            _b64url(json.dumps(header, separators=(",", ":")).encode("utf-8"))
            + "."
            + _b64url(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
        )
        sig = hmac.new(
            self.secret_key.encode("utf-8"), signing_input.encode("ascii"), hashlib.sha256
        ).digest()
        return signing_input + "." + _b64url(sig)
