# ============================================================
# Binance USDT-M 선물 FOCUS 매니저 — FocusManager 상속
# ------------------------------------------------------------
# [2026-06-23 부모] "바이낸스도 선물+현물" — 선물(USDT-M perp).
# FocusManager 두뇌(스캔/진입/청산/점수/가드/존버)는 거래소 추상화(client + 시드 메서드:
#   get_market_tickers/get_instrument_info/get_available_margin/list_open_positions)로
#   돌아가도록 리팩터됨(2026-06-23). 이 서브클래스는 client/state 경로/live-sync 만 Binance.
#
# Bybit 선물(FocusManager) ↔ Binance 선물 차이:
#   - client = BinanceTradeClient(category="linear")  (_make_real_client override)
#   - config/state = runtime/binance_futures/focus_config.json  (Bybit와 자본·상태 격리)
#   - _sync_with_bybit_inner: Bybit판은 position row 인라인 stopLoss/unrealisedPnl 의존 →
#     Binance positionRisk엔 그 필드 없음(SL=별도 주문). client.list_open_positions()
#     기반 정규화 sync 로 override(고스트 제거 + qty/평단 동기화 + TP/SL 재배치).
#
# ★ 첫 기동은 paper 강제(검증 안 된 거래소 관측부터). 부모가 UI/runtime 으로 live 전환.
# ============================================================
from __future__ import annotations

import logging
import os
from typing import Any

from app.manager.focus_manager import FocusManager

logger = logging.getLogger(__name__)


class BinanceFuturesManager(FocusManager):
    """Binance USDT-M 선물 FOCUS. FocusManager 와 동일 로직, client/state/live-sync 만 Binance."""

    def __init__(self, system: Any = None):
        # ★★ [감사 bug#2] _config_path 를 super().__init__() *전에* 확정.
        #   FocusManager.__init__ 이 _load_config()/_sync_with_bybit() 를 호출하는데,
        #   경로가 Bybit(runtime/focus_config.json)인 채로 돌면 LIVE 에서 Bybit 포지션을
        #   고스트로 오인 제거 후 Bybit state 파일을 덮어쓸 수 있음 → 주력 엔진 손상.
        #   (FocusManager L1678 은 getattr(self,'_config_path',None) or CONFIG_PATH 로 보존.)
        try:
            from app.core.runtime_paths import RuntimePaths
            self._config_path = RuntimePaths(exchange="binance_futures").custom("focus_config.json")
        except Exception:
            self._config_path = os.path.join("runtime", "binance_futures", "focus_config.json")
            os.makedirs(os.path.dirname(self._config_path), exist_ok=True)
        # ★★ [감사 bug#1] paper 안전장치 — 선물 FocusManager 는 config.paper 를 *안 읽는다*
        #   (live 판정은 env _is_live_mode 뿐). 따라서 실효 게이트 = _force_paper:
        #   _get_client() 가 이 값을 보고 명시 opt-in(BINANCE_FUTURES_LIVE=1) 전엔 항상
        #   FocusDryClient(가상주문) 반환 → 미검증 거래소 실탄 0. super() 전에 세팅(초기 sync 보호).
        self._force_paper = os.getenv("BINANCE_FUTURES_LIVE", "0").strip().lower() not in ("1", "true", "yes")
        # ★★ [감사 bug#5] 거래소별 장부 격리 — super() 전에 세팅하면 부모 init 의 get_journal 이
        #   Binance 전용 장부(runtime/binance_futures/journal.jsonl)를 잡음. Bybit 장부와 PnL/거래/
        #   reentry·coin_repeat 게이트 완전 분리. (라우터 읽기도 같은 경로 사용.)
        self._journal_path = os.path.join(os.path.dirname(self._config_path), "journal.jsonl")
        # ★★ [감사 high#4] 일별 스냅샷 디렉터리 격리 — 부모 _maybe_reset_daily 가 snap_dir 없이
        #   save_snapshot 하면 Bybit 와 같은 runtime/focus_daily_snapshots/{date}.json 을 덮어씀.
        #   super() 전에 세팅(getattr 로 부모가 읽음). 라우터 읽기(_BINANCE_FUT_SNAP_DIR)와 동일 경로.
        self._snap_dir = os.path.join(os.path.dirname(self._config_path), "daily_snapshots")
        _fresh = not os.path.exists(self._config_path)
        super().__init__(system=system)
        # ★ [2026-06-23 부모 "Bybit값 1회 복사 후 독립"] 첫 기동(Binance config 파일 없음)에만
        #   Bybit 의 현재 튜닝된 config 를 시드 → 수개월 연구된 기준값으로 시작(생짜 기본값 reverse 함정 회피).
        #   ★ state(포지션/zone)는 복사 안 함 — Bybit 라이브 포지션 유출 방지(config 섹션만).
        #   이후엔 자기 파일 로드 → 거래소별 독립 튜닝.
        if _fresh:
            try:
                self._seed_config_from_bybit()
            except Exception as exc:
                logger.warning("[BINANCE_FUT] Bybit config 시드 실패(기본값 유지): %s", exc)
        if self._force_paper:
            logger.info("[BINANCE_FUT] paper 강제 (BINANCE_FUTURES_LIVE 미설정) — 실주문 0, 관측만")

    def _seed_config_from_bybit(self):
        """Bybit 선물 config 의 'config' 섹션만 복사해 Binance 초기값으로 (1회). state 제외."""
        import json
        from app.manager.focus_manager import CONFIG_PATH as _BYBIT_CFG
        if not os.path.exists(_BYBIT_CFG):
            logger.info("[BINANCE_FUT] Bybit config 파일 없음 — 코드 기본값으로 시작")
            return
        with open(_BYBIT_CFG, "r", encoding="utf-8") as f:
            data = json.load(f)
        cfg = data.get("config") if isinstance(data, dict) else None
        if not cfg:
            return
        self.update_config(cfg)   # type 강제 + hasattr 필터 (state/미존재 키 무시)
        self._save_config()        # Binance 전용 경로에 영속(config + 빈 state)
        logger.info("[BINANCE_FUT] Bybit config 시드 완료 (%d fields) → %s", len(cfg), self._config_path)

    def _make_real_client(self):
        from app.integrations.binance_trade import BinanceTradeClient
        return BinanceTradeClient(category="linear")

    def _get_current_price(self, market: str):
        """[감사 bug#6] 가격을 Binance client 에서 직접 — 부모 _get_current_price 는
        Bybit WS-fed price_store(bybit:SYMBOL)를 먼저 읽어 공유 심볼(BTCUSDT 등) Binance
        포지션을 Bybit 가격으로 평가함. Binance/Bybit perp 가격은 장중 발산 → PnL/TP/SL 오평가.
        client 직접 last price + 2초 로컬 캐시(틱 폭주 방지)."""
        import time as _t
        mk = (market or "").upper()
        box = getattr(self, "_bfut_px_cache", None)
        if box is None:
            box = self._bfut_px_cache = {}
        hit = box.get(mk)
        now = _t.time()
        if hit and (now - hit[0]) < 2.0:
            return hit[1]
        try:
            p = float(self._get_client()._linear_last_price(mk) or 0)
            if p > 0:
                box[mk] = (now, p)
                return p
        except Exception as exc:
            logger.debug("[BINANCE_FUT] price %s failed: %s", mk, exc)
        return hit[1] if hit else None

    def _sync_with_bybit_inner(self):
        """Binance live 동기화 — client.list_open_positions() 정규화 기반.
        Bybit판(인라인 stopLoss/unrealisedPnl)과 달리 SL은 별도 주문이라, 보유 포지션의
        qty/평단만 거래소 기준으로 맞추고 고스트(로컬엔 있는데 거래소엔 없음)를 제거한다.
        TP/SL은 set_trading_stop(STOP_MARKET·TAKE_PROFIT_MARKET)로 재배치."""
        try:
            client = self._get_client()
            rows = client.list_open_positions()
            live_map = {}
            for bp in rows:
                try:
                    sz = abs(float(bp.get("size", 0) or 0))
                    if sz > 0:
                        live_map[str(bp.get("symbol", "")).upper()] = {
                            "size": sz,
                            "side": bp.get("side", ""),
                            "avgPrice": float(bp.get("avgPrice", 0) or 0),
                        }
                except (TypeError, ValueError):
                    continue

            # 고스트 제거 (로컬 보유인데 거래소엔 없음) + 보유분 qty/평단 동기화
            synced = []
            for pos in self.positions:
                mkt = pos.market.upper()
                if mkt not in live_map:
                    logger.warning("[BINANCE_FUT] SYNC: %s 거래소에 없음 → 고스트 제거", mkt)
                    continue
                bp = live_map[mkt]
                if abs(pos.qty - bp["size"]) > 1e-8:
                    logger.warning("[BINANCE_FUT] SYNC: %s qty %.6f → %.6f (거래소 실제)",
                                   mkt, pos.qty, bp["size"])
                pos.qty = bp["size"]
                if bp["avgPrice"] > 0:
                    pos.entry_price = bp["avgPrice"]
                synced.append(pos)
            self.positions = synced
            self.position = self.positions[0] if self.positions else None

            # 보유 포지션 TP/SL 거래소 재배치 (증발 방지 — STOP/TP_MARKET closePosition)
            # ★ [감사 medium#3] 값이 *바뀐* 포지션만 재배치 — 매 sync(≈30s) 무조건 cancel→recreate 하면
            #   ① 취소~재생성 사이 서버측 SL 부재 윈도우 ② algo 주문 rate-limit 폭주 ③ 주문ID churn.
            #   동일 SL/TP 이미 박혀있으면(confirmed) 건너뜀.
            for pos in self.positions:
                try:
                    tp = pos.tp2 if getattr(pos, "partial_done", False) else pos.tp1
                    _key = (round(float(tp or 0), 10), round(float(pos.sl or 0), 10))
                    if getattr(pos, "_tpsl_set_key", None) == _key and getattr(pos, "_tp_sl_confirmed", False):
                        continue
                    client.set_trading_stop(pos.market, take_profit=tp, stop_loss=pos.sl)
                    pos._tp_sl_confirmed = True
                    pos._tpsl_set_key = _key
                except Exception as ts_exc:
                    logger.warning("[BINANCE_FUT] SYNC set_trading_stop %s failed: %s", pos.market, ts_exc)

            self._save_config()
            logger.info("[BINANCE_FUT] SYNC complete: %d positions", len(self.positions))
        except Exception as exc:
            logger.warning("[BINANCE_FUT] sync failed: %s", exc)
