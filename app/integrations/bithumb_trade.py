# ============================================================
# Bithumb spot trading client — extends UpbitTradeClient
# ------------------------------------------------------------
# [2026-06-17 owner] Another copy of the same bot on a Bithumb account.
# Verified empirically that the Bithumb 2.0 API is ~100% compatible with Upbit:
#   - public /v1/market/all·candles/minutes·ticker·orderbook = same format as Upbit
#     (market code "KRW-BTC", down to the market_warning field)
#   - private = JWT(Bearer) + /v1/orders·/v1/accounts = same structure as Upbit
#     except Bithumb requires a timestamp added to the JWT payload.
# → Only override base URL + _make_jwt(timestamp). The other ~20 methods
#   (get_kline/get_tickers/get_orderbook/market_buy/sell/get_balance/
#    accounts/place_order/get_order/cancel_order/wait_order/get_market_warnings)
#   are all reused via inheritance. The brain (manager/strategy/scoring/UI) stays
#   the same — just swap the client.
#
# ⚠️ To verify after issuing keys (private could not be tested empirically):
#   - whether /v1/orders params·response exactly match Upbit (market/side/ord_type/volume/price)
#   - /v1/accounts balance response fields (currency/balance/locked/avg_buy_price)
#   - MIN_ORDER_KRW (Bithumb min order — assume same 5000 as Upbit for now, adjust if different)
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
    """Bithumb API error (same signature as UpbitAPIError — compatible with brain-code except blocks)."""


class BithumbTradeClient(UpbitTradeClient):
    """Bithumb 2.0 spot client. /v1/ compatible with Upbit, so inherited almost as-is.
    Only base URL + JWT(timestamp added) differ. Market code/candle/orderbook/order formats identical."""

    API_BASE = BITHUMB_API_BASE
    # ★ Bithumb 2.0 POST (/v1/orders etc.) must send the body as JSON (Upbit=form).
    #   A form body returns 401 invalid_query_payload — query_hash (based on urlencode) is the same, only the body differs.
    _post_as_json = True

    def _make_jwt(self, query: Optional[Dict[str, Any]] = None) -> str:
        """Bithumb JWT = same as Upbit (access_key·nonce·query_hash SHA512) + timestamp added."""
        if not self.access_key or not self.secret_key:
            raise BithumbAPIError("Bithumb API key/secret not configured")
        payload: Dict[str, Any] = {
            "access_key": self.access_key,
            "nonce": str(_uuid.uuid4()),
            "timestamp": int(time.time() * 1000),   # ★ required by Bithumb (the only difference from Upbit)
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
