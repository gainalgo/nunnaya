# ============================================================
# Binance USDT-M futures FOCUS manager — extends FocusManager
# ------------------------------------------------------------
# [2026-06-23 owner] "Binance too — futures + spot" — futures (USDT-M perp).
# The FocusManager brain (scan/entry/exit/scoring/guards/long-hold) was refactored
#   (2026-06-23) to run on an exchange abstraction (client + seed methods:
#   get_market_tickers/get_instrument_info/get_available_margin/list_open_positions).
#   This subclass only changes the client / state paths / live-sync for Binance.
#
# Bybit futures (FocusManager) vs Binance futures differences:
#   - client = BinanceTradeClient(category="linear")  (_make_real_client override)
#   - config/state = runtime/binance_futures/focus_config.json  (capital/state isolated from Bybit)
#   - _sync_with_bybit_inner: the Bybit version relies on inline stopLoss/unrealisedPnl on the
#     position row, but Binance positionRisk has no such field (SL = a separate order). So it is
#     overridden with a normalized sync based on client.list_open_positions()
#     (ghost removal + qty/avg-price sync + TP/SL re-placement).
#
# ★ First boot forces paper (observe an unvalidated exchange first). The owner flips to live via UI/runtime.
# ============================================================
from __future__ import annotations

import logging
import os
from typing import Any

from app.manager.focus_manager import FocusManager

logger = logging.getLogger(__name__)


class BinanceFuturesManager(FocusManager):
    """Binance USDT-M futures FOCUS. Same logic as FocusManager; only client/state/live-sync are Binance."""

    def __init__(self, system: Any = None):
        # ★★ [audit bug#2] Fix _config_path *before* super().__init__().
        #   FocusManager.__init__ calls _load_config()/_sync_with_bybit(); if it runs while the path
        #   still points at Bybit (runtime/focus_config.json), in LIVE it could mistake Bybit positions
        #   for ghosts, remove them, and overwrite the Bybit state file → damaging the main engine.
        #   (FocusManager L1678 preserves it via getattr(self,'_config_path',None) or CONFIG_PATH.)
        try:
            from app.core.runtime_paths import RuntimePaths
            self._config_path = RuntimePaths(exchange="binance_futures").custom("focus_config.json")
        except Exception:
            self._config_path = os.path.join("runtime", "binance_futures", "focus_config.json")
            os.makedirs(os.path.dirname(self._config_path), exist_ok=True)
        # ★★ [audit bug#1] paper safety — the futures FocusManager does *not* read config.paper
        #   (live is decided only by env _is_live_mode). So the effective gate is _force_paper:
        #   _get_client() honours this value and, until explicit opt-in (BINANCE_FUTURES_LIVE=1),
        #   always returns FocusDryClient (virtual orders) → zero real funds on an unvalidated exchange.
        #   Set before super() (protects the initial sync).
        self._force_paper = os.getenv("BINANCE_FUTURES_LIVE", "0").strip().lower() not in ("1", "true", "yes")
        # ★★ [audit bug#5] per-exchange journal isolation — setting this before super() makes the parent
        #   init's get_journal pick up the Binance-only journal (runtime/binance_futures/journal.jsonl).
        #   Fully separates PnL / trades / reentry & coin_repeat gates from the Bybit journal.
        #   (The router reads from the same path.)
        self._journal_path = os.path.join(os.path.dirname(self._config_path), "journal.jsonl")
        # ★★ [audit high#4] daily snapshot directory isolation — if the parent's _maybe_reset_daily
        #   calls save_snapshot without snap_dir, it overwrites the same runtime/focus_daily_snapshots/{date}.json
        #   that Bybit uses. Set before super() (the parent reads it via getattr). Same path the router reads
        #   (_BINANCE_FUT_SNAP_DIR).
        self._snap_dir = os.path.join(os.path.dirname(self._config_path), "daily_snapshots")
        _fresh = not os.path.exists(self._config_path)
        super().__init__(system=system)
        # ★ [2026-06-23 owner "copy Bybit values once, then independent"] Only on first boot (no Binance
        #   config file) seed Binance with Bybit's currently-tuned config → start from months-researched
        #   baseline values (avoids the raw-default reverse trap).
        #   ★ state (positions/zones) is NOT copied — prevents leaking Bybit live positions (config section only).
        #   Afterwards it loads its own file → independent per-exchange tuning.
        if _fresh:
            try:
                self._seed_config_from_bybit()
            except Exception as exc:
                logger.warning("[BINANCE_FUT] Bybit config seed failed (keeping defaults): %s", exc)
        if self._force_paper:
            logger.info("[BINANCE_FUT] paper forced (BINANCE_FUTURES_LIVE unset) — 0 real orders, observe only")

    def _seed_config_from_bybit(self):
        """Copy only the 'config' section of the Bybit futures config as Binance's initial values (once). Excludes state."""
        import json
        from app.manager.focus_manager import CONFIG_PATH as _BYBIT_CFG
        if not os.path.exists(_BYBIT_CFG):
            logger.info("[BINANCE_FUT] Bybit config file not found — starting from code defaults")
            return
        with open(_BYBIT_CFG, "r", encoding="utf-8") as f:
            data = json.load(f)
        cfg = data.get("config") if isinstance(data, dict) else None
        if not cfg:
            return
        self.update_config(cfg)   # type coercion + hasattr filter (ignores state / non-existent keys)
        self._save_config()        # persist to the Binance-only path (config + empty state)
        logger.info("[BINANCE_FUT] Bybit config seeded (%d fields) → %s", len(cfg), self._config_path)

    def _make_real_client(self):
        from app.integrations.binance_trade import BinanceTradeClient
        return BinanceTradeClient(category="linear")

    def _get_current_price(self, market: str):
        """[audit bug#6] Read price directly from the Binance client — the parent _get_current_price
        reads the Bybit WS-fed price_store (bybit:SYMBOL) first, which would value a Binance position on a
        shared symbol (BTCUSDT etc.) at the Bybit price. Binance/Bybit perp prices diverge intraday →
        mis-valued PnL/TP/SL. Client direct last price + 2s local cache (avoids tick storms)."""
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
        """Binance live sync — based on the normalized client.list_open_positions().
        Unlike the Bybit version (inline stopLoss/unrealisedPnl), SL is a separate order here, so it only
        aligns held positions' qty/avg-price to the exchange and removes ghosts (held locally but not on the
        exchange). TP/SL is re-placed via set_trading_stop (STOP_MARKET / TAKE_PROFIT_MARKET)."""
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

            # Ghost removal (held locally but not on the exchange) + qty/avg-price sync for held positions
            synced = []
            for pos in self.positions:
                mkt = pos.market.upper()
                if mkt not in live_map:
                    logger.warning("[BINANCE_FUT] SYNC: %s not on exchange → removing ghost", mkt)
                    continue
                bp = live_map[mkt]
                if abs(pos.qty - bp["size"]) > 1e-8:
                    logger.warning("[BINANCE_FUT] SYNC: %s qty %.6f → %.6f (exchange actual)",
                                   mkt, pos.qty, bp["size"])
                pos.qty = bp["size"]
                if bp["avgPrice"] > 0:
                    pos.entry_price = bp["avgPrice"]
                synced.append(pos)
            self.positions = synced
            self.position = self.positions[0] if self.positions else None

            # Re-place TP/SL on the exchange for held positions (prevents evaporation — STOP/TP_MARKET closePosition)
            # ★ [audit medium#3] Only re-place positions whose values *changed* — re-running cancel→recreate
            #   on every sync (≈30s) unconditionally causes ① an SL-absent window between cancel and recreate
            #   ② algo-order rate-limit storms ③ order-ID churn. Skip if the same SL/TP is already placed (confirmed).
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
