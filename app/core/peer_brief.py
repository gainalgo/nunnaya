# ============================================================
# File: app/core/peer_brief.py
# Autocoin OS — Peer Brief (peer server guard)
# ------------------------------------------------------------
# Codification of the owner's 2026-05-18 insight [[feedback_time_based_mechanism_skeptic]]:
#   "time-independent mechanism = peer servers + aggregated news opinion + existing guards"
#
# Flow:
#   1. Each server exposes its own recent 30-min loss exits + held positions + health via GET /peer/brief
#   2. A background polling task periodically polls the peer servers in PEER_SERVER_URLS -> in-memory cache
#   3. focus_manager calls check_peer_block() just before entry -> reject when a peer has an SL/holding on the same coin+direction
#   4. In paper mode, log only instead of rejecting (for validation)
#
# Safe fallback:
#   - peer server timeout/no response -> cache not refreshed -> guard inactive (standalone mode)
#   - PEER_BRIEF_ENABLED=false -> guard itself disabled (default)
#   - one server's wrong SL -> auto window expiry after 30 min
#
# ENV:
#   PEER_SERVER_URLS=http://host1:8010,http://host2:8010   (CSV, peer server base URLs — empty = no polling)
#   PEER_BRIEF_ENABLED=true|false      (default True — spirit of owner's 5-18 insight, settled)
#   PEER_BRIEF_PAPER=true|false        (default False — owner's 5-31 decision, live from the start)
#   PEER_SERVER_ID=server-a             (own identifier, included in brief response, hostname when unset)
#   PEER_BRIEF_POLL_INTERVAL_SEC=20    (peer server polling interval)
#   PEER_BRIEF_TIMEOUT_SEC=0.5         (single polling timeout)
#   PEER_BRIEF_SL_WINDOW_MIN=30        (peer SL block window — minutes)
#   PEER_BRIEF_TOKEN=                  (optional, lightweight auth header between peers)
# ============================================================
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import requests

from app.core.constants import env_bool, env_float, env_int

logger = logging.getLogger(__name__)

# ── Runtime override store ──
# Values saved from the UI take precedence over .env. Falls back to .env when key is absent.
# Disk: runtime/peer_brief_state.json
_STATE_PATH = os.path.join("runtime", "peer_brief_state.json")
_STATE: Dict = {}
_STATE_LOCK = threading.Lock()


def _load_state() -> Dict:
    if not os.path.exists(_STATE_PATH):
        return {}
    try:
        with open(_STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError) as exc:
        logger.warning("[PEER] state load failed: %s", exc)
        return {}


def _save_state(state: Dict) -> None:
    os.makedirs(os.path.dirname(_STATE_PATH) or ".", exist_ok=True)
    with _STATE_LOCK:
        try:
            with open(_STATE_PATH, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2, ensure_ascii=False)
        except OSError as exc:
            logger.warning("[PEER] state save failed: %s", exc)
            return
        _STATE.clear()
        _STATE.update(state)


def update_state(patch: Dict) -> Dict:
    """Partial update + disk save."""
    merged = dict(_STATE)
    merged.update(patch)
    _save_state(merged)
    return merged


def get_state() -> Dict:
    return dict(_STATE)


# Load once at module import (takes precedence over env)
_STATE.update(_load_state())


# ── Schema ──
@dataclass
class PeerLossExit:
    symbol: str
    direction: str   # "LONG" / "SHORT"
    ts: float
    reason: str      # exit_reason
    pnl_net: float


@dataclass
class PeerWinExit:
    """A spot where a peer server hit BE / TP / trailing exit / profitable exit — a positive 'that was a good spot' signal."""
    symbol: str
    direction: str
    ts: float
    reason: str      # exit_reason ("TP1", "TP2", "trailing", "BE_*", etc.)
    pnl_net: float


@dataclass
class PeerActivePosition:
    symbol: str
    direction: str
    age_min: float = 0.0        # holding elapsed (min) — longer = unresolved (no TP/SL hit yet)
    peak_pnl_pct: float = 0.0   # max profit% reached while holding (never green => ~0/negative = struggling)
    pnl_pct: float = 0.0        # current unrealized PnL% (price based, owner 2026-06-07 — shows extent of loss)
    pnl_usdt: float = 0.0       # current unrealized net PnL USDT (notional×% − round-trip 0.11%, same as Positions panel Net, 2026-06-07)


@dataclass
class PeerNearMiss:
    """An entry that passed the score but was blocked at a last-stage gate/slot/pair-lock — an 'almost made it' spot (owner 2026-06-07)."""
    symbol: str
    direction: str
    score: float        # guard_score final (score that passed the threshold)
    reason: str         # the gate that blocked (final_30m15m / slot_full / pair_block etc.)
    ts: float
    price: float = 0.0  # price at block time — for post-hoc review monitoring


@dataclass
class PeerBrief:
    server_id: str
    ts: float                                                        # brief creation time
    recent_losses: List[PeerLossExit] = field(default_factory=list)
    recent_wins: List[PeerWinExit] = field(default_factory=list)
    active_positions: List[PeerActivePosition] = field(default_factory=list)
    recent_near_miss: List[PeerNearMiss] = field(default_factory=list)
    health: Dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "schema": 2,
            "server_id": self.server_id,
            "ts": self.ts,
            "recent_losses": [
                {"symbol": x.symbol, "direction": x.direction, "ts": x.ts,
                 "reason": x.reason, "pnl_net": x.pnl_net}
                for x in self.recent_losses
            ],
            "recent_wins": [
                {"symbol": x.symbol, "direction": x.direction, "ts": x.ts,
                 "reason": x.reason, "pnl_net": x.pnl_net}
                for x in self.recent_wins
            ],
            "active_positions": [
                {"symbol": x.symbol, "direction": x.direction,
                 "age_min": x.age_min, "peak_pnl_pct": x.peak_pnl_pct, "pnl_pct": x.pnl_pct,
                 "pnl_usdt": x.pnl_usdt}
                for x in self.active_positions
            ],
            "recent_near_miss": [
                {"symbol": x.symbol, "direction": x.direction,
                 "score": x.score, "reason": x.reason, "ts": x.ts, "price": x.price}
                for x in self.recent_near_miss
            ],
            "health": self.health,
        }

    @staticmethod
    def from_dict(d: dict) -> Optional["PeerBrief"]:
        if not isinstance(d, dict):
            return None
        try:
            return PeerBrief(
                server_id=str(d.get("server_id") or ""),
                ts=float(d.get("ts") or 0.0),
                recent_losses=[
                    PeerLossExit(
                        symbol=str(x.get("symbol") or "").upper(),
                        direction=str(x.get("direction") or "").upper(),
                        ts=float(x.get("ts") or 0.0),
                        reason=str(x.get("reason") or ""),
                        pnl_net=float(x.get("pnl_net") or 0.0),
                    )
                    for x in (d.get("recent_losses") or [])
                    if isinstance(x, dict)
                ],
                recent_wins=[
                    PeerWinExit(
                        symbol=str(x.get("symbol") or "").upper(),
                        direction=str(x.get("direction") or "").upper(),
                        ts=float(x.get("ts") or 0.0),
                        reason=str(x.get("reason") or ""),
                        pnl_net=float(x.get("pnl_net") or 0.0),
                    )
                    for x in (d.get("recent_wins") or [])
                    if isinstance(x, dict)
                ],
                active_positions=[
                    PeerActivePosition(
                        symbol=str(x.get("symbol") or "").upper(),
                        direction=str(x.get("direction") or "").upper(),
                        age_min=float(x.get("age_min") or 0.0),
                        peak_pnl_pct=float(x.get("peak_pnl_pct") or 0.0),
                        pnl_pct=float(x.get("pnl_pct") or 0.0),
                        pnl_usdt=float(x.get("pnl_usdt") or 0.0),
                    )
                    for x in (d.get("active_positions") or [])
                    if isinstance(x, dict)
                ],
                recent_near_miss=[
                    PeerNearMiss(
                        symbol=str(x.get("symbol") or "").upper(),
                        direction=str(x.get("direction") or "").upper(),
                        score=float(x.get("score") or 0.0),
                        reason=str(x.get("reason") or ""),
                        ts=float(x.get("ts") or 0.0),
                        price=float(x.get("price") or 0.0),
                    )
                    for x in (d.get("recent_near_miss") or [])
                    if isinstance(x, dict)
                ],
                health=dict(d.get("health") or {}),
            )
        except (KeyError, AttributeError, TypeError, ValueError) as exc:
            logger.debug("[PEER] brief parse failed: %s", exc)
            return None


# ── Env helpers ──
def _self_url_hints() -> set:
    """Set of hints identifying self server (hostname / local IP / localhost)."""
    hints = {"localhost", "127.0.0.1", "0.0.0.0", "::1"}
    try:
        import socket
        hn = (socket.gethostname() or "").lower()
        if hn:
            hints.add(hn)
            hints.add(hn.split(".")[0])
        # outward-facing local IP (no actual packet sent)
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                s.connect(("8.8.8.8", 80))
                hints.add(s.getsockname()[0])
            finally:
                s.close()
        except OSError:
            pass
    except OSError:
        pass
    # friendly-name tokens for self-hint come only from the per-server .env (PEER_SERVER_ID) (+ hostname above).
    # ⚠ runtime (_STATE.server_id) may have another server's value copied/mis-saved into it (e.g. 'ByBit_ServerB'
    #   on server-a); adding it to self-hint would wrongly self-skip a real peer (home) via the 'home' token -> excluded.
    #   A friendly env name (ByBit_ServerA) also has its subdomain label (server-a) caught as a token, so a self-poll
    #   on one's own domain (hairpin) is auto-skipped. 'bybit'/numeric tokens don't overlap peer labels, so harmless.
    env_sid = (os.getenv("PEER_SERVER_ID", "") or "").strip().lower()
    if env_sid:
        hints.add(env_sid)
        for tok in re.split(r"[^a-z0-9]+", env_sid):
            # labels only (alpha start + 3 chars+) — prevents '1'/'01' numeric tokens from mis-skipping IP labels
            if len(tok) >= 3 and tok[0].isalpha():
                hints.add(tok)
    return hints


def _is_self_url(url: str, hints: set) -> bool:
    try:
        from urllib.parse import urlparse
        host = (urlparse(url).hostname or "").lower()
        if not host:
            return False
        # full host (server-b.example.com) or first label (server-b) matching a self hint = self.
        # -> setting PEER_SERVER_ID=server-b pre-skips a self-poll on one's own public domain (Cloudflare
        #   hairpin, >10s hang -> permanently stale). Exact label match, so no mis-skip of server-c etc.
        return host in hints or host.split(".")[0] in hints
    except (ValueError, AttributeError):
        return False


# log self-skip only when the result changes (get_peer_urls is called on every /peer/settings·poll -> avoid spam)
_LAST_SKIP_LOGGED: frozenset = frozenset()


def get_peer_urls() -> List[str]:
    # 1) runtime override (UI input) takes precedence
    state_urls = _STATE.get("urls")
    if isinstance(state_urls, list) and state_urls:
        raw = ",".join(str(u) for u in state_urls if u)
    else:
        raw = (os.getenv("PEER_SERVER_URLS", "") or "").strip()
    if not raw:
        return []
    # robustness: CSV split + auto-strip 'PEER_SERVER_URLS=' prefix
    # (handles the accident of the prefix being copied while the owner merges into one line)
    tokens = []
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        # auto-remove prefix (case-insensitive)
        low = token.lower()
        if low.startswith("peer_server_urls="):
            token = token.split("=", 1)[1].strip()
        if not token:
            continue
        token = token.rstrip("/")
        if token and token not in tokens:
            tokens.append(token)
    # auto-skip self — match own hostname / local IP / PEER_SERVER_ID
    hints = _self_url_hints()
    skipped = [u for u in tokens if _is_self_url(u, hints)]
    if skipped:
        global _LAST_SKIP_LOGGED
        sk = frozenset(skipped)
        if sk != _LAST_SKIP_LOGGED:  # once, only when changed — avoid INFO spam on every call
            logger.info("[PEER] self-URL auto-skip: %s (hints=%s)", skipped, sorted(hints))
            _LAST_SKIP_LOGGED = sk
    return [u for u in tokens if not _is_self_url(u, hints)]


def get_server_id() -> str:
    # identity: the per-server .env (PEER_SERVER_ID) is the top priority.
    # ⚠ runtime override frequently has another server's value copied/mis-saved (e.g. 'ByBit_ServerB' in
    #   server-a's runtime) masking the .env -> unlike other tunables, for server_id alone env wins.
    sid = (os.getenv("PEER_SERVER_ID", "") or "").strip()
    if sid:
        return sid
    # runtime override (dashboard-set directly) only when env absent. Ignore an auto-fallback hostname pin.
    try:
        import socket
        hn = socket.gethostname() or ""
    except OSError:
        hn = ""
    v = (_STATE.get("server_id") or "").strip() if isinstance(_STATE.get("server_id"), str) else ""
    if v and v.lower() != hn.lower():
        return v
    return hn or "unknown"


def get_peer_token() -> str:
    # the token is .env-only for security (not exposed in UI)
    return (os.getenv("PEER_BRIEF_TOKEN", "") or "").strip()


def get_cf_access_headers() -> dict:
    """Cloudflare Access Service Token headers — to pass through when a peer server is protected by Access.

    Headers are added automatically when CF_ACCESS_CLIENT_ID + CF_ACCESS_CLIENT_SECRET are set in .env.
    Returns an empty dict when unset (compatible with Access-unprotected environments).
    """
    cid = (os.getenv("CF_ACCESS_CLIENT_ID", "") or "").strip()
    csec = (os.getenv("CF_ACCESS_CLIENT_SECRET", "") or "").strip()
    if cid and csec:
        return {"CF-Access-Client-Id": cid, "CF-Access-Client-Secret": csec}
    return {}


def is_enabled() -> bool:
    if "enabled" in _STATE:
        return bool(_STATE["enabled"])
    return env_bool("PEER_BRIEF_ENABLED", default=True)


def is_paper() -> bool:
    if "paper" in _STATE:
        return bool(_STATE["paper"])
    return env_bool("PEER_BRIEF_PAPER", default=False)


def get_sl_window_min() -> int:
    v = _STATE.get("sl_window_min")
    if isinstance(v, (int, float)) and v > 0:
        return max(1, int(v))
    return max(1, env_int("PEER_BRIEF_SL_WINDOW_MIN", default=30))


def get_poll_interval_sec() -> float:
    v = _STATE.get("poll_interval_sec")
    if isinstance(v, (int, float)) and v >= 2:
        return max(2.0, float(v))
    return max(2.0, env_float("PEER_BRIEF_POLL_INTERVAL_SEC", 20.0))


def get_timeout_sec() -> float:
    # runtime(_STATE) > .env > default. Unified priority with the other getters.
    # 2026-06-05: the code default is 3.0, but .env's PEER_BRIEF_TIMEOUT_SEC=0.5 overrode it
    #   and neutralized the fix -> corrected the .env value to 3.0 + added the _STATE override.
    #   0.5s can't even hold the http->https 301 redirect round-trip (server-b ~0.85s), causing staleness.
    v = _STATE.get("timeout_sec")
    if isinstance(v, (int, float)) and v > 0:
        return max(0.1, float(v))
    return max(0.1, env_float("PEER_BRIEF_TIMEOUT_SEC", 3.0))


def get_peer_win_window_min() -> int:
    """Reference window (min) for peer win signals. Separate from the SL window (usually shorter)."""
    v = _STATE.get("peer_win_window_min")
    if isinstance(v, (int, float)) and v > 0:
        return max(1, int(v))
    return max(1, env_int("PEER_BRIEF_WIN_WINDOW_MIN", default=15))


def get_peer_win_bonus() -> float:
    """Conviction bonus on a peer win match. 0 = disabled."""
    v = _STATE.get("peer_win_bonus")
    if isinstance(v, (int, float)):
        return max(0.0, min(50.0, float(v)))
    return max(0.0, min(50.0, env_float("PEER_BRIEF_WIN_BONUS", 5.0)))


# ── Cache (in-memory) ──
_BRIEF_CACHE: Dict[str, PeerBrief] = {}        # url -> latest brief
_LAST_POLL_TS: Dict[str, float] = {}            # url -> last successful polling ts
_LAST_FAIL_TS: Dict[str, float] = {}            # url -> last failed polling ts
_CACHE_LOCK = asyncio.Lock()


# ── Build own brief (for router response) ──
def build_my_brief(system=None) -> PeerBrief:
    """Build the current server's brief.

    - loss (pnl_net < 0) EXITs within the recent sl_window_min minutes
    - currently held positions (symbol, direction)
    - bot health (focus_active, focus_ready, last_fill_ts)
    """
    now = time.time()
    server_id = get_server_id()
    # ★ [2026-06-11] recent_losses spans up to the fleet dir_fail window (4h) — to share time-staggered (50-min) losses.
    #   soft check_peer_sl_caution re-filters with its own 30-min window, so no impact.
    window_sec = max(get_sl_window_min(), get_peer_fleet_dirfail_window_min()) * 60.0
    cutoff = now - window_sec

    recent_losses: List[PeerLossExit] = []
    recent_wins: List[PeerWinExit] = []
    win_cutoff = now - get_peer_win_window_min() * 60.0
    try:
        from app.manager.trade_journal import journal as _jnl
        page = _jnl.get_trades(limit=200, strategy="FOCUS", include_blocked=False)
        for rec in (page.get("trades") or []):
            if rec.get("event") != "EXIT":
                continue
            ts = float(rec.get("ts") or 0.0)
            pnl = float(rec.get("pnl_net") or 0.0)
            sym = str(rec.get("market") or "").upper()
            dirn = str(rec.get("direction") or "").upper()
            reason = str(rec.get("exit_reason") or "")
            # loss (within SL window)
            if pnl < 0 and ts >= cutoff:
                recent_losses.append(PeerLossExit(
                    symbol=sym, direction=dirn, ts=ts, reason=reason, pnl_net=pnl,
                ))
            # profitable exit (within win window) — "this spot was good" positive signal
            # profit (pnl > 0) or a BE-reached reason pattern
            elif (pnl > 0 or _is_win_reason(reason)) and ts >= win_cutoff:
                recent_wins.append(PeerWinExit(
                    symbol=sym, direction=dirn, ts=ts, reason=reason, pnl_net=pnl,
                ))
    except (KeyError, AttributeError, TypeError, ValueError) as exc:
        logger.debug("[PEER] my recent_losses/wins build failed: %s", exc)

    active: List[PeerActivePosition] = []
    near_miss: List[PeerNearMiss] = []
    health: Dict = {}
    try:
        if system is not None:
            fm = getattr(system, "focus_manager", None)
            if fm is not None:
                for pos in (getattr(fm, "positions", None) or []):
                    sym = (getattr(pos, "market", "") or "").upper()
                    dirn = (getattr(pos, "direction", "") or "").upper()
                    if sym and dirn in ("LONG", "SHORT"):
                        # derive a "struggling" signal from stored fields alone, no current-price lookup:
                        #   age = holding elapsed (min), peak_pnl = max profit% reached while holding (peak_profit_price)
                        _ets = float(getattr(pos, "entry_ts", 0.0) or 0.0)
                        _age = (now - _ets) / 60.0 if _ets > 0 else 0.0
                        _ep = float(getattr(pos, "entry_price", 0.0) or 0.0)
                        _pk = float(getattr(pos, "peak_profit_price", 0.0) or 0.0)
                        if _ep > 0 and _pk > 0:
                            _peak_pnl = ((_pk / _ep - 1.0) * 100.0) if dirn == "LONG" else ((1.0 - _pk / _ep) * 100.0)
                        else:
                            _peak_pnl = 0.0
                        # current unrealized PnL% (cached current price — owner's "extent of loss", 2026-06-07)
                        _cur = 0.0
                        try:
                            _cur = float(fm._get_current_price(sym) or 0.0)
                        except Exception:  # noqa: BLE001
                            _cur = 0.0
                        if _ep > 0 and _cur > 0:
                            _pnl = ((_cur / _ep - 1.0) * 100.0) if dirn == "LONG" else ((1.0 - _cur / _ep) * 100.0)
                        else:
                            _pnl = 0.0
                        # net PnL USDT — notional×% − round-trip 0.11% (same formula as Positions panel Net)
                        _qty = float(getattr(pos, "qty", 0.0) or 0.0)
                        _notional = _ep * _qty
                        _pnl_usdt = _notional * (_pnl / 100.0) - _notional * 0.0011 if _notional > 0 else 0.0
                        active.append(PeerActivePosition(
                            symbol=sym, direction=dirn,
                            age_min=round(_age, 1), peak_pnl_pct=round(_peak_pnl, 2),
                            pnl_pct=round(_pnl, 2), pnl_usdt=round(_pnl_usdt, 2),
                        ))
                health["focus_active"] = len(active)
                # near-miss: passed the score but blocked at the last stage (in-memory ring buffer, no journal parse — owner 2026-06-07)
                try:
                    for nm in list(getattr(fm, "_recent_near_miss", None) or []):
                        near_miss.append(PeerNearMiss(
                            symbol=str(nm.get("symbol") or "").upper(),
                            direction=str(nm.get("direction") or "").upper(),
                            score=float(nm.get("score") or 0.0),
                            reason=str(nm.get("reason") or ""),
                            ts=float(nm.get("ts") or 0.0),
                            price=float(nm.get("price") or 0.0),
                        ))
                except (AttributeError, TypeError, ValueError):
                    pass
                try:
                    health["focus_ready"] = int(getattr(fm, "_ready_count", 0) or 0)
                except (TypeError, ValueError):
                    health["focus_ready"] = 0
            try:
                health["last_fill_ts"] = float(getattr(system, "_last_fill_ts", 0.0) or 0.0)
            except (TypeError, ValueError):
                health["last_fill_ts"] = 0.0
    except (AttributeError, TypeError) as exc:
        logger.debug("[PEER] my active/health build failed: %s", exc)

    return PeerBrief(
        server_id=server_id,
        ts=now,
        recent_losses=recent_losses,
        recent_wins=recent_wins,
        active_positions=active,
        recent_near_miss=near_miss,
        health=health,
    )


def _is_win_reason(reason: str) -> bool:
    """Whether the exit reason is a 'success' pattern — TP / trailing / BE reached etc."""
    r = (reason or "").lower()
    if not r:
        return False
    return any(k in r for k in ("tp1", "tp2", "tp_", "trail", "be_", "breakeven", "profit"))


# ── Polling (background) ──
async def _fetch_one(url: str, timeout: float) -> Optional[PeerBrief]:
    """Poll a single peer server. None when self-response is detected."""
    headers = {}
    tok = get_peer_token()
    if tok:
        headers["X-Peer-Token"] = tok
    # Cloudflare Access Service Token (passes through when a peer server is protected by Access)
    headers.update(get_cf_access_headers())

    full_url = f"{url}/peer/brief"

    def _do():
        return requests.get(full_url, headers=headers, timeout=timeout)

    try:
        resp = await asyncio.wait_for(asyncio.to_thread(_do), timeout=timeout + 0.3)
        if resp.status_code != 200:
            logger.debug("[PEER] %s → HTTP %d", full_url, resp.status_code)
            return None
        data = resp.json()
        brief = PeerBrief.from_dict(data)
        # detect self-polling — block caching when brief.server_id equals own ID
        if brief is not None:
            my_id = get_server_id()
            if my_id and brief.server_id and brief.server_id == my_id:
                logger.info("[PEER] self-detected via server_id: url=%s id=%s — skipped", url, my_id)
                return None
        return brief
    except (requests.RequestException, asyncio.TimeoutError, ValueError) as exc:
        logger.debug("[PEER] fetch fail %s: %s", url, exc)
        return None


async def _poll_loop():
    """Background polling task — started in main.py lifespan."""
    logger.info("[PEER] poll loop entering — interval=%.1fs timeout=%.2fs",
                get_poll_interval_sec(), get_timeout_sec())
    while True:
        try:
            urls = get_peer_urls()
            enabled = is_enabled()
            if enabled and urls:
                timeout = get_timeout_sec()
                tasks = [_fetch_one(u, timeout) for u in urls]
                results = await asyncio.gather(*tasks, return_exceptions=False)
                now = time.time()
                async with _CACHE_LOCK:
                    for url, brief in zip(urls, results):
                        if brief is not None:
                            _BRIEF_CACHE[url] = brief
                            _LAST_POLL_TS[url] = now
                        else:
                            _LAST_FAIL_TS[url] = now
                            # delete expired cache — don't block on a stale brief
                            last_ok = _LAST_POLL_TS.get(url, 0.0)
                            stale_sec = get_poll_interval_sec() * 4.0
                            if last_ok > 0 and (now - last_ok) > stale_sec:
                                _BRIEF_CACHE.pop(url, None)
        except (RuntimeError, OSError) as exc:
            logger.warning("[PEER] poll iteration failed: %s", exc)

        try:
            await asyncio.sleep(get_poll_interval_sec())
        except asyncio.CancelledError:
            logger.info("[PEER] poll loop cancelled")
            return


_poll_task: Optional[asyncio.Task] = None


def start_poll_loop() -> Optional[asyncio.Task]:
    """Called from main.py lifespan startup."""
    global _poll_task
    if _poll_task is not None and not _poll_task.done():
        return _poll_task
    try:
        loop = asyncio.get_running_loop()
        _poll_task = loop.create_task(_poll_loop())
        logger.info("[PEER] poll loop started — urls=%s enabled=%s paper=%s server_id=%s",
                    get_peer_urls(), is_enabled(), is_paper(), get_server_id())
        return _poll_task
    except RuntimeError as exc:
        logger.warning("[PEER] poll loop start failed (no running loop): %s", exc)
        return None


def stop_poll_loop():
    global _poll_task
    if _poll_task is not None and not _poll_task.done():
        _poll_task.cancel()
    _poll_task = None


# ── Gate (sync, called by focus_manager) ──
def check_peer_block(symbol: str, direction: str) -> Tuple[bool, str]:
    """(True, reason) when a peer server has a recent SL or current holding on the same coin+direction.

    Returns:
        (True, reason)  -> block (actual reject)
        (False, "")     -> pass
        (False, "paper:...")  -> paper mode = match present but not blocked (for logging)

    enabled=False -> always (False, "")
    """
    if not is_enabled():
        return False, ""
    sym = (symbol or "").upper()
    dirn = (direction or "").upper()
    if not sym or dirn not in ("LONG", "SHORT"):
        return False, ""

    window_sec = get_sl_window_min() * 60.0
    now = time.time()
    cutoff = now - window_sec

    matched_sl: Optional[Tuple[str, PeerLossExit]] = None
    matched_active: Optional[Tuple[str, PeerActivePosition]] = None

    # snapshot (copy dict without lock) — race condition harmless (peer polling is a separate task)
    for url, brief in list(_BRIEF_CACHE.items()):
        if brief is None:
            continue
        for loss in brief.recent_losses:
            if loss.symbol != sym or loss.direction != dirn:
                continue
            if loss.ts < cutoff:
                continue
            matched_sl = (brief.server_id or url, loss)
            break
        if matched_sl:
            break
        for ap in brief.active_positions:
            if ap.symbol == sym and ap.direction == dirn:
                matched_active = (brief.server_id or url, ap)
                break
        if matched_active:
            break

    if matched_sl is None and matched_active is None:
        return False, ""

    if matched_sl:
        peer_id, loss = matched_sl
        age_min = int((now - loss.ts) / 60.0)
        reason = f"peer_sl:{peer_id}:{sym}/{dirn} {age_min}m ago net={loss.pnl_net:+.2f}"
    else:
        peer_id, ap = matched_active
        reason = f"peer_holds:{peer_id}:{sym}/{dirn}"

    if is_paper():
        return False, f"paper:{reason}"
    return True, reason


def check_peer_win_bonus(symbol: str, direction: str) -> Tuple[float, str]:
    """Peer positive signal — (bonus, source) when a peer server has a win on the same coin+direction within the last N minutes.

    Returns:
        (0.0, "")        — no match / disabled
        (bonus, source)  — bonus + source (e.g. "BiBit_Home BNBUSDT/LONG 3m ago TP1")

    The bonus is applied *only once* (the nearest win). No double-counting even if multiple peers match.
    """
    if not is_enabled():
        return 0.0, ""
    bonus = get_peer_win_bonus()
    if bonus <= 0:
        return 0.0, ""
    sym = (symbol or "").upper()
    dirn = (direction or "").upper()
    if not sym or dirn not in ("LONG", "SHORT"):
        return 0.0, ""

    window_sec = get_peer_win_window_min() * 60.0
    now = time.time()
    cutoff = now - window_sec

    best: Optional[Tuple[str, PeerWinExit]] = None  # (server_id, win)
    for url, brief in list(_BRIEF_CACHE.items()):
        if brief is None:
            continue
        for win in brief.recent_wins:
            if win.symbol != sym or win.direction != dirn:
                continue
            if win.ts < cutoff:
                continue
            if best is None or win.ts > best[1].ts:
                best = (brief.server_id or url, win)

    if best is None:
        return 0.0, ""
    peer_id, win = best
    age_min = int((now - win.ts) / 60.0)
    source = f"{peer_id} {sym}/{dirn} {age_min}m ago {win.reason or 'win'}"
    return bonus, source


def get_peer_sl_penalty() -> float:
    """Conviction (guard_score) penalty on a peer recent-SL match on the same coin+direction. 0 = disabled.
    ⚠ Not a hard block — if the core indicators (including a current rebound) are strong, entry proceeds past this penalty.
    Owner 2026-06-06: "the peer is a reference to think once more before the own logic enters."."""
    v = _STATE.get("peer_sl_penalty")
    if isinstance(v, (int, float)):
        return max(0.0, min(50.0, float(v)))
    return max(0.0, min(50.0, env_float("PEER_BRIEF_SL_PENALTY", 8.0)))


def check_peer_sl_caution(symbol: str, direction: str) -> Tuple[float, str]:
    """Peer recent SL (loss exit) on the same coin+direction -> (penalty, source). For a soft penalty.

    Returns:
        (0.0, "")        — no match / disabled
        (penalty, source)
    ⚠ Not a hard block (the old check_peer_block is retired). Ignores 'holdings', looks only at recent SLs.
    "Re-entry into an unchanged SL spot" is filtered by the core guards (Reentry/dir_fail_window) + this penalty,
    while an "indicator rebound" has high core conviction that exceeds the penalty, so the owner's "rebound bonus" is handled naturally.
    """
    if not is_enabled():
        return 0.0, ""
    penalty = get_peer_sl_penalty()
    if penalty <= 0:
        return 0.0, ""
    sym = (symbol or "").upper()
    dirn = (direction or "").upper()
    if not sym or dirn not in ("LONG", "SHORT"):
        return 0.0, ""

    window_sec = get_sl_window_min() * 60.0
    now = time.time()
    cutoff = now - window_sec

    best: Optional[Tuple[str, PeerLossExit]] = None  # (server_id, loss) — most recent
    for url, brief in list(_BRIEF_CACHE.items()):
        if brief is None:
            continue
        for loss in brief.recent_losses:
            if loss.symbol != sym or loss.direction != dirn:
                continue
            if loss.ts < cutoff:
                continue
            if best is None or loss.ts > best[1].ts:
                best = (brief.server_id or url, loss)

    if best is None:
        return 0.0, ""
    peer_id, loss = best
    age_min = int((now - loss.ts) / 60.0)
    source = f"{peer_id} {sym}/{dirn} {age_min}m ago net={loss.pnl_net:+.2f}"
    return penalty, source


def get_peer_struggle_penalty() -> float:
    """Conviction penalty when a peer has held the same coin+direction for N+ min without ever properly going green (struggling/oscillating).
    Owner's 2026-06-06 TON case: prevents another server from re-entering the same direction while one server's position
    flipped direction after entry and is seesawing for 5~10 min. ⚠ Not a hard block, a soft nudge — leaves healthy profit riding untouched."""
    v = _STATE.get("peer_struggle_penalty")
    if isinstance(v, (int, float)):
        return max(0.0, min(50.0, float(v)))
    return max(0.0, min(50.0, env_float("PEER_BRIEF_STRUGGLE_PENALTY", 8.0)))


def get_peer_struggle_age_min() -> float:
    """Struggle decision — held N+ minutes (runtime _STATE > env). For UI tuning."""
    v = _STATE.get("peer_struggle_age_min")
    if isinstance(v, (int, float)) and v > 0:
        return max(1.0, float(v))
    return max(1.0, env_float("PEER_BRIEF_STRUGGLE_AGE_MIN", 5.0))


def get_peer_struggle_peak_pct() -> float:
    """Struggle decision — if the max profit% reached while holding is below this, it 'never went green' (struggling). (runtime _STATE > env). For UI tuning."""
    v = _STATE.get("peer_struggle_peak_pct")
    if isinstance(v, (int, float)):
        return float(v)
    return env_float("PEER_BRIEF_STRUGGLE_PEAK_PCT", 0.3)


def check_peer_struggle_caution(symbol: str, direction: str) -> Tuple[float, str]:
    """A peer 'holding while struggling' the same coin+direction (age >= N min AND peak_pnl < threshold = never properly went green)
    -> (penalty, source). Profit riding (high peak_pnl) or a fresh entry (short age) is not penalized.

    age/peak_pnl ride along in the brief's PeerActivePosition (no current-price lookup needed). An old brief (field 0)
    has age_min=0 so is auto-excluded -> backward-compat safe.
    """
    if not is_enabled():
        return 0.0, ""
    penalty = get_peer_struggle_penalty()
    if penalty <= 0:
        return 0.0, ""
    sym = (symbol or "").upper()
    dirn = (direction or "").upper()
    if not sym or dirn not in ("LONG", "SHORT"):
        return 0.0, ""

    age_min_req = get_peer_struggle_age_min()
    peak_max = get_peer_struggle_peak_pct()  # green above this % = healthy -> excluded

    for url, brief in list(_BRIEF_CACHE.items()):
        if brief is None:
            continue
        for ap in brief.active_positions:
            if ap.symbol != sym or ap.direction != dirn:
                continue
            if ap.age_min >= age_min_req and ap.peak_pnl_pct < peak_max:
                src = f"{brief.server_id or url} {sym}/{dirn} {int(ap.age_min)}m peak{ap.peak_pnl_pct:+.2f}%"
                return penalty, src
    return 0.0, ""


def get_peer_conflict_penalty() -> float:
    """Conviction penalty when a peer is healthily holding the 'opposite direction' (reached profit) and I try to enter against it.
    Owner's 2026-06-07 self-conflict (ZEC Home LONG <-> server-c SHORT): an opposite holding is a hedge so the loss shrinks
    but it eats fees for 0 profit -> be cautious. ⚠ But if the other side is 'struggling' (low peak = direction turning over) no penalty —
    then my opposite entry is a 'turn catch' (regime flip) and may actually be right. Not a hard block, a soft nudge. 0=disabled.
    runtime _STATE > env. (for UI tuning)"""
    v = _STATE.get("peer_conflict_penalty")
    if isinstance(v, (int, float)):
        return max(0.0, min(50.0, float(v)))
    return max(0.0, min(50.0, env_float("PEER_BRIEF_CONFLICT_PENALTY", 8.0)))


def check_peer_conflict_caution(symbol: str, direction: str) -> Tuple[float, str]:
    """A peer is healthily holding the 'opposite direction' (peak >= threshold = reached profit) and I enter against it
    -> (penalty, source). For caution against self-conflict (capital cancels out, fees yield 0 profit). Soft penalty.

    ★ Asymmetric (owner 2026-06-07): if the other side is 'struggling' (peak < threshold = never properly went green), penalty 0 —
      the other side's direction is turning over -> my opposite entry is a 'turn catch', so don't block it ([[feedback_regime_flip_asymmetric]]).
    Healthy-decision threshold = reuse get_peer_struggle_peak_pct() (same baseline as struggle, unified into one knob).
    peak_pnl_pct rides along in the brief's PeerActivePosition. An old brief (field 0) has peak 0 < threshold -> auto-excluded (safe).
    """
    if not is_enabled():
        return 0.0, ""
    penalty = get_peer_conflict_penalty()
    if penalty <= 0:
        return 0.0, ""
    sym = (symbol or "").upper()
    dirn = (direction or "").upper()
    if not sym or dirn not in ("LONG", "SHORT"):
        return 0.0, ""
    opp = "SHORT" if dirn == "LONG" else "LONG"

    peak_min = get_peer_struggle_peak_pct()  # other side's peak above this % = healthy (in profit) -> caution on conflict / below = struggling -> pass

    best: Optional[Tuple[str, PeerActivePosition]] = None  # healthiest (largest peak) opposite position
    for url, brief in list(_BRIEF_CACHE.items()):
        if brief is None:
            continue
        for ap in brief.active_positions:
            if ap.symbol != sym or ap.direction != opp:
                continue
            if ap.peak_pnl_pct < peak_min:
                continue  # other side struggling = turn signal -> no penalty (turn-catch)
            if best is None or ap.peak_pnl_pct > best[1].peak_pnl_pct:
                best = (brief.server_id or url, ap)

    if best is None:
        return 0.0, ""
    peer_id, ap = best
    src = f"{peer_id} holds {sym}/{opp} peak{ap.peak_pnl_pct:+.2f}% (vs my {dirn})"
    return penalty, src


# ── 🌊 [2026-06-15 owner] fleet crowding — soft penalty when peers hold the same coin+'same' direction ──
#   Today's TRUMP LONG: 4 servers entered almost simultaneously within 4 min (341~345 min ago) -> diversification broke -> all evenly −3.5%.
#   The existing struggle penalty has the condition 'age>=5 min AND peak<0.3%' (struggling), so it doesn't count a freshly-entered
#   healthy holding -> a simultaneous-entry hole. Owner: "reference not only peer SLs but also *current holdings*" -> if a peer is
#   currently holding the same direction, that itself is a crowding signal. ⚠ Not a hard block (owner "must enter even after seeing"), soft penalty only —
#   enters past it when core conviction is strong. graduated (N points per peer server, up to cap). default OFF (opt-in).
def get_peer_crowding_penalty() -> float:
    """Conviction penalty per peer server holding the same coin+same direction (graduated). 0 = disabled (default)."""
    v = _STATE.get("peer_crowding_penalty")
    if isinstance(v, (int, float)):
        return max(0.0, min(50.0, float(v)))
    return max(0.0, min(50.0, env_float("PEER_BRIEF_CROWDING_PENALTY", 0.0)))  # ★ default OFF


def get_peer_crowding_cap() -> float:
    """Total cap (points) for the crowding penalty. Even with many servers crowding, deducts no more than this."""
    v = _STATE.get("peer_crowding_cap")
    if isinstance(v, (int, float)) and v > 0:
        return max(1.0, min(50.0, float(v)))
    return max(1.0, min(50.0, env_float("PEER_BRIEF_CROWDING_CAP", 12.0)))


def check_peer_crowding_caution(symbol: str, direction: str) -> Tuple[float, str]:
    """Number of peer servers *currently holding* the same coin+'same' direction -> (penalty, source). Soft penalty.

    Unlike struggle/conflict, no age·peak condition — counts even a 'freshly-entered healthy holding' as crowding (blocks simultaneous entry).
    penalty = min(cap, holding-server-count × per). Empty peer cache -> 0 = fail-open. Same direction only (opposite is handled by conflict).
    """
    if not is_enabled():
        return 0.0, ""
    per = get_peer_crowding_penalty()
    if per <= 0:
        return 0.0, ""
    sym = (symbol or "").upper()
    dirn = (direction or "").upper()
    if not sym or dirn not in ("LONG", "SHORT"):
        return 0.0, ""

    servers = set()
    for url, brief in list(_BRIEF_CACHE.items()):
        if brief is None:
            continue
        for ap in brief.active_positions:
            if ap.symbol == sym and ap.direction == dirn:
                servers.add(brief.server_id or url)
                break
    n = len(servers)
    if n <= 0:
        return 0.0, ""
    penalty = min(get_peer_crowding_cap(), n * per)
    src = f"fleet {sym}/{dirn} crowding: {n} peer servers holding (−{per:.0f} per server, cap {get_peer_crowding_cap():.0f})"
    return penalty, src


# ── 🛡️ [2026-06-11 owner] fleet dir_fail — block on fleet-cumulative losses for the same coin·direction ──
#   Today's VELVET LONG: server-b 8:59->server-a 9:43->home 9:48 sequential (50-min stagger) string of deaths.
#   The existing check_peer_sl_caution (soft 8 pts·30 min) failed because ① window 30 min < 49-min stagger ② soft can't stop a strong signal.
#   Owner: "they couldn't see each other's loss records so entered freely" -> fleet (peers+me) cumulative N losses on the same coin+direction -> hard block.
def get_peer_fleet_dirfail_enabled() -> bool:
    v = _STATE.get("fleet_dirfail_enabled")
    if isinstance(v, bool):
        return v
    return env_bool("PEER_BRIEF_FLEET_DIRFAIL_ENABLED", default=False)  # ★ default OFF


def get_peer_fleet_dirfail_max() -> int:
    """Fleet-cumulative losses >= N = block (peers+me combined). default 2 = allow first attempt, block from the 2nd."""
    v = _STATE.get("fleet_dirfail_max")
    if isinstance(v, (int, float)) and v > 0:
        return max(1, int(v))
    return max(1, env_int("PEER_BRIEF_FLEET_DIRFAIL_MAX", default=2))


def get_peer_fleet_dirfail_window_min() -> int:
    """Fleet loss accumulation window (min) — unified with local dir_fail 4h (240 min). Covers time-staggered losses."""
    v = _STATE.get("fleet_dirfail_window_min")
    if isinstance(v, (int, float)) and v > 0:
        return max(1, int(v))
    return max(1, env_int("PEER_BRIEF_FLEET_DIRFAIL_WINDOW_MIN", default=240))


def check_peer_direction_fail_caution(symbol: str, direction: str, local_count: int = 0) -> Tuple[bool, str]:
    """Fleet (peer servers + me) cumulative losses on the same coin+direction >= max -> (blocked, source). For a hard block.

    local_count = number of losses on the same coin+direction within the window from my own journal (computed and passed by focus_manager).
    peer_count = number of losses on the same coin+direction within the window across all peer brief.recent_losses.
    sum >= max -> block entry. In paper/dry, don't block (log only). Empty _BRIEF_CACHE -> peer 0 = fail-open.
    """
    if not is_enabled() or not get_peer_fleet_dirfail_enabled():
        return False, ""
    sym = (symbol or "").upper()
    dirn = (direction or "").upper()
    if not sym or dirn not in ("LONG", "SHORT"):
        return False, ""
    fail_max = get_peer_fleet_dirfail_max()
    window_sec = get_peer_fleet_dirfail_window_min() * 60.0
    now = time.time()
    cutoff = now - window_sec
    peer_count = 0
    servers = set()
    for url, brief in list(_BRIEF_CACHE.items()):
        if brief is None:
            continue
        for loss in brief.recent_losses:
            if loss.symbol == sym and loss.direction == dirn and loss.ts >= cutoff:
                peer_count += 1
                servers.add(brief.server_id or url)
    total = peer_count + max(0, int(local_count))
    if total < fail_max:
        return False, ""
    win_h = get_peer_fleet_dirfail_window_min() / 60.0
    src = (f"fleet {sym}/{dirn} {total} fails>={fail_max} "
           f"(peer {peer_count}/{len(servers)} servers + local {local_count}, {win_h:.0f}h window)")
    if is_paper():
        return False, f"paper:{src}"
    return True, src


def get_cache_snapshot() -> dict:
    """For dashboard/debug — current cache state."""
    now = time.time()
    peers = []
    for url in get_peer_urls():
        brief = _BRIEF_CACHE.get(url)
        last_ok = _LAST_POLL_TS.get(url, 0.0)
        last_fail = _LAST_FAIL_TS.get(url, 0.0)
        peers.append({
            "url": url,
            "server_id": brief.server_id if brief else "",
            "ok_age_sec": int(now - last_ok) if last_ok > 0 else -1,
            "fail_age_sec": int(now - last_fail) if last_fail > 0 else -1,
            "recent_losses": len(brief.recent_losses) if brief else 0,
            "recent_wins": len(brief.recent_wins) if brief else 0,
            "active_positions": len(brief.active_positions) if brief else 0,
            "stale": brief is None,
            # ── details (Peer Brief Scanner, owner 2026-06-07) — expose what's already in the cache ──
            "positions": [
                {"symbol": p.symbol, "direction": p.direction, "age_min": p.age_min,
                 "peak_pnl_pct": p.peak_pnl_pct, "pnl_pct": p.pnl_pct, "pnl_usdt": p.pnl_usdt}
                for p in (brief.active_positions if brief else [])
            ],
            "near_miss": [
                {"symbol": n.symbol, "direction": n.direction, "score": n.score,
                 "reason": n.reason, "ts": n.ts, "price": n.price,
                 "age_min": round((now - n.ts) / 60.0, 1) if n.ts else 0}
                for n in (brief.recent_near_miss if brief else [])
            ],
            "losses": [
                {"symbol": x.symbol, "direction": x.direction, "pnl_net": x.pnl_net,
                 "age_min": round((now - x.ts) / 60.0, 1) if x.ts else 0}
                for x in (brief.recent_losses if brief else [])
            ],
            "wins": [
                {"symbol": x.symbol, "direction": x.direction, "pnl_net": x.pnl_net,
                 "age_min": round((now - x.ts) / 60.0, 1) if x.ts else 0}
                for x in (brief.recent_wins if brief else [])
            ],
        })
    return {
        "enabled": is_enabled(),
        "paper": is_paper(),
        "server_id": get_server_id(),
        "sl_window_min": get_sl_window_min(),
        "poll_interval_sec": get_poll_interval_sec(),
        "timeout_sec": get_timeout_sec(),
        "peer_win_window_min": get_peer_win_window_min(),
        "peer_win_bonus": get_peer_win_bonus(),
        "peer_sl_penalty": get_peer_sl_penalty(),
        "peer_struggle_penalty": get_peer_struggle_penalty(),
        "peer_struggle_age_min": get_peer_struggle_age_min(),
        "peer_struggle_peak_pct": get_peer_struggle_peak_pct(),
        "peer_conflict_penalty": get_peer_conflict_penalty(),
        "peer_crowding_penalty": get_peer_crowding_penalty(),
        "peer_crowding_cap": get_peer_crowding_cap(),
        "peers": peers,
    }
