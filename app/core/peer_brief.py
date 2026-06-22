# ============================================================
# File: app/core/peer_brief.py
# Autocoin OS — Peer Brief (옆 서버 가드)
# ------------------------------------------------------------
# 부모님 2026-05-18 통찰 [[feedback_time_based_mechanism_skeptic]]:
#   "시간 무관 메커니즘 = 옆 서버 + 뉴스 종합 의견 + 기존 가드"
# 의 코드화.
#
# 흐름:
#   1. 각 서버는 GET /peer/brief 로 자기 최근 30분 손실 청산 + 보유 포지션 + health 노출
#   2. 백그라운드 polling task 가 PEER_SERVER_URLS 의 옆 서버를 주기적으로 polling → 메모리 캐시
#   3. focus_manager 진입 직전 check_peer_block() 호출 → 같은 코인+방향 옆 SL/보유 시 reject
#   4. paper mode 시 reject 대신 로그만 (검증용)
#
# 안전 fallback:
#   - 옆 서버 timeout/응답 없음 → 캐시 미갱신 → 가드 미작동 (단독 모드)
#   - PEER_BRIEF_ENABLED=false → 가드 자체 비활성 (default)
#   - 한 서버 잘못된 SL → 30분 자동 윈도우 만료
#
# ENV:
#   PEER_SERVER_URLS=http://host1:8010,http://host2:8010   (CSV, 옆 서버 base URL — 비면 polling X)
#   PEER_BRIEF_ENABLED=true|false      (default True — 부모님 5-18 통찰 정신, 정착)
#   PEER_BRIEF_PAPER=true|false        (default False — 부모님 5-31 결정, 처음부터 실가동)
#   PEER_SERVER_ID=server-a             (자기 식별자, brief 응답에 포함, 미설정 시 hostname)
#   PEER_BRIEF_POLL_INTERVAL_SEC=20    (옆 서버 polling 주기)
#   PEER_BRIEF_TIMEOUT_SEC=0.5         (단일 polling timeout)
#   PEER_BRIEF_SL_WINDOW_MIN=30        (옆 SL 차단 윈도우 — 분)
#   PEER_BRIEF_TOKEN=                  (옵션, peer 간 약식 인증 헤더)
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
# UI 에서 저장한 값이 .env 보다 우선. 키가 없으면 .env fallback.
# 디스크: runtime/peer_brief_state.json
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
    """부분 update + 디스크 저장."""
    merged = dict(_STATE)
    merged.update(patch)
    _save_state(merged)
    return merged


def get_state() -> Dict:
    return dict(_STATE)


# 모듈 import 시 1회 load (env 보다 우선)
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
    """옆 서버가 BE 도달 / TP / trailing exit / 흑자 청산한 자리 — '거기 좋았다' 양의 신호."""
    symbol: str
    direction: str
    ts: float
    reason: str      # exit_reason ("TP1", "TP2", "trailing", "BE_*" 등)
    pnl_net: float


@dataclass
class PeerActivePosition:
    symbol: str
    direction: str
    age_min: float = 0.0        # 보유 경과(분) — 길수록 미해결(TP/SL 못 침)
    peak_pnl_pct: float = 0.0   # 보유 중 최고 도달 수익% (한 번도 못 green이면 ~0/음수 = 헤맴)
    pnl_pct: float = 0.0        # 현재 미실현 손익% (가격 기준, 2026-06-07 부모 — 손실 정도 표시)
    pnl_usdt: float = 0.0       # 현재 미실현 순손익 USDT (notional×% − 왕복0.11%, Positions 패널 Net과 동일, 2026-06-07)


@dataclass
class PeerNearMiss:
    """점수는 합격인데 막판 게이트/슬롯/페어락에서 차단된 진입 — '되려다 안 된' 자리 (2026-06-07 부모)."""
    symbol: str
    direction: str
    score: float        # guard_score final (임계 통과한 점수)
    reason: str         # 막은 게이트 (final_30m15m / slot_full / pair_block 등)
    ts: float
    price: float = 0.0  # 차단 당시 가격 — 사후판정 관제용


@dataclass
class PeerBrief:
    server_id: str
    ts: float                                                        # brief 생성 시각
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
    """자기 서버 식별 hint 집합 (hostname / 로컬 IP / localhost)."""
    hints = {"localhost", "127.0.0.1", "0.0.0.0", "::1"}
    try:
        import socket
        hn = (socket.gethostname() or "").lower()
        if hn:
            hints.add(hn)
            hints.add(hn.split(".")[0])
        # 외부 향 로컬 IP (실제 패킷 안 보냄)
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
    # self-hint 친화적 이름 토큰은 per-서버 .env(PEER_SERVER_ID) 만 (+ 위 hostname).
    # ⚠ runtime(_STATE.server_id) 은 다른 서버 값이 복사/오저장될 수 있어(예: server-a 에
    #   'ByBit_ServerB') self-hint 에 넣으면 진짜 peer(home)를 'home' 토큰으로 잘못 self-skip → 제외.
    #   env 친화적 이름(ByBit_ServerA)도 subdomain 라벨(server-a)이 토큰으로 잡혀 자기
    #   도메인 self-poll(hairpin) 자동 skip. 'bybit'/숫자 토큰은 peer 라벨과 안 겹쳐 무해.
    env_sid = (os.getenv("PEER_SERVER_ID", "") or "").strip().lower()
    if env_sid:
        hints.add(env_sid)
        for tok in re.split(r"[^a-z0-9]+", env_sid):
            # 라벨만 (글자시작 + 3자↑) — '1'/'01' 숫자 토큰이 IP 라벨 오skip 방지
            if len(tok) >= 3 and tok[0].isalpha():
                hints.add(tok)
    return hints


def _is_self_url(url: str, hints: set) -> bool:
    try:
        from urllib.parse import urlparse
        host = (urlparse(url).hostname or "").lower()
        if not host:
            return False
        # full host (server-b.example.com) 또는 첫 라벨 (server-b) 이 self hint 와 일치 = 자기.
        # → PEER_SERVER_ID=server-b 로 두면 자기 public 도메인 self-poll(Cloudflare hairpin,
        #   >10s hang → 영구 stale) 을 사전에 skip. 라벨 정확 일치라 server-c 등 오skip 없음.
        return host in hints or host.split(".")[0] in hints
    except (ValueError, AttributeError):
        return False


# self-skip 로그는 결과가 바뀔 때만 (get_peer_urls 가 /peer/settings·poll 마다 호출 → spam 방지)
_LAST_SKIP_LOGGED: frozenset = frozenset()


def get_peer_urls() -> List[str]:
    # 1) runtime override (UI 입력) 우선
    state_urls = _STATE.get("urls")
    if isinstance(state_urls, list) and state_urls:
        raw = ",".join(str(u) for u in state_urls if u)
    else:
        raw = (os.getenv("PEER_SERVER_URLS", "") or "").strip()
    if not raw:
        return []
    # robustness: CSV 분리 + 'PEER_SERVER_URLS=' prefix 자동 strip
    # (부모님 한 줄 합치는 과정에서 prefix 복사되는 사고 대응)
    tokens = []
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        # prefix 자동 제거 (case-insensitive)
        low = token.lower()
        if low.startswith("peer_server_urls="):
            token = token.split("=", 1)[1].strip()
        if not token:
            continue
        token = token.rstrip("/")
        if token and token not in tokens:
            tokens.append(token)
    # 자기 자동 skip — 자기 hostname / 로컬 IP / PEER_SERVER_ID 매칭
    hints = _self_url_hints()
    skipped = [u for u in tokens if _is_self_url(u, hints)]
    if skipped:
        global _LAST_SKIP_LOGGED
        sk = frozenset(skipped)
        if sk != _LAST_SKIP_LOGGED:  # 바뀔 때만 1회 — 매 호출 INFO spam 방지
            logger.info("[PEER] self-URL auto-skip: %s (hints=%s)", skipped, sorted(hints))
            _LAST_SKIP_LOGGED = sk
    return [u for u in tokens if not _is_self_url(u, hints)]


def get_server_id() -> str:
    # 식별(identity)은 per-서버 .env(PEER_SERVER_ID) 가 1순위.
    # ⚠ runtime override 는 다른 서버 값이 복사/오저장돼(예: server-a runtime 에 'ByBit_ServerB')
    #   .env 를 가리는 사고가 잦음 → 다른 tunable 과 달리 server_id 만은 env 우선.
    sid = (os.getenv("PEER_SERVER_ID", "") or "").strip()
    if sid:
        return sid
    # env 없을 때만 runtime override(대시보드 직접 지정). hostname 자동fallback pin 은 무시.
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
    # token 은 보안 차원에서 .env 만 (UI 노출 X)
    return (os.getenv("PEER_BRIEF_TOKEN", "") or "").strip()


def get_cf_access_headers() -> dict:
    """Cloudflare Access Service Token 헤더 — 옆 서버가 Access 로 보호되면 통과용.

    CF_ACCESS_CLIENT_ID + CF_ACCESS_CLIENT_SECRET .env 설정 시 자동 헤더 추가.
    미설정 시 빈 dict 리턴 (Access 미보호 환경 호환).
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
    # runtime(_STATE) > .env > default. 다른 getter 와 우선순위 통일.
    # 2026-06-05: 코드 default 는 3.0 이지만 .env 의 PEER_BRIEF_TIMEOUT_SEC=0.5 가
    #   이를 덮어써 fix 가 무력화됐던 사고 → .env 값 3.0 정정 + _STATE override 추가.
    #   0.5초는 http→https 301 redirect 왕복(server-b ~0.85s)도 못 담아 stale 유발.
    v = _STATE.get("timeout_sec")
    if isinstance(v, (int, float)) and v > 0:
        return max(0.1, float(v))
    return max(0.1, env_float("PEER_BRIEF_TIMEOUT_SEC", 3.0))


def get_peer_win_window_min() -> int:
    """옆 win 신호 참조 윈도우 (분). SL 윈도우와 별개 (보통 짧게)."""
    v = _STATE.get("peer_win_window_min")
    if isinstance(v, (int, float)) and v > 0:
        return max(1, int(v))
    return max(1, env_int("PEER_BRIEF_WIN_WINDOW_MIN", default=15))


def get_peer_win_bonus() -> float:
    """옆 win 매칭 시 conviction 가점. 0 = 비활성."""
    v = _STATE.get("peer_win_bonus")
    if isinstance(v, (int, float)):
        return max(0.0, min(50.0, float(v)))
    return max(0.0, min(50.0, env_float("PEER_BRIEF_WIN_BONUS", 5.0)))


# ── Cache (메모리) ──
_BRIEF_CACHE: Dict[str, PeerBrief] = {}        # url → 최근 brief
_LAST_POLL_TS: Dict[str, float] = {}            # url → 마지막 polling 성공 ts
_LAST_FAIL_TS: Dict[str, float] = {}            # url → 마지막 polling 실패 ts
_CACHE_LOCK = asyncio.Lock()


# ── 자기 brief 생성 (router 응답용) ──
def build_my_brief(system=None) -> PeerBrief:
    """현재 서버의 brief 를 만든다.

    - 최근 sl_window_min 분 안 손실 (pnl_net < 0) EXIT
    - 현재 보유 포지션 (symbol, direction)
    - 봇 health (focus_active, focus_ready, last_fill_ts)
    """
    now = time.time()
    server_id = get_server_id()
    # ★ [2026-06-11] recent_losses 는 함대 dir_fail 윈도우(4h)까지 담는다 — 시간차(50분) 손실 공유.
    #   soft check_peer_sl_caution 은 자기 30분 윈도우로 재필터하므로 영향 없음.
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
            # 손실 (SL 윈도우 안)
            if pnl < 0 and ts >= cutoff:
                recent_losses.append(PeerLossExit(
                    symbol=sym, direction=dirn, ts=ts, reason=reason, pnl_net=pnl,
                ))
            # 흑자 청산 (win 윈도우 안) — "이 자리 좋았다" 양의 신호
            # 흑자 (pnl > 0) 또는 BE 도달 reason 패턴
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
                        # 현재가 조회 없이 저장 필드만으로 "고전" 신호 산출:
                        #   age = 보유 경과(분), peak_pnl = 보유 중 최고 도달 수익%(peak_profit_price)
                        _ets = float(getattr(pos, "entry_ts", 0.0) or 0.0)
                        _age = (now - _ets) / 60.0 if _ets > 0 else 0.0
                        _ep = float(getattr(pos, "entry_price", 0.0) or 0.0)
                        _pk = float(getattr(pos, "peak_profit_price", 0.0) or 0.0)
                        if _ep > 0 and _pk > 0:
                            _peak_pnl = ((_pk / _ep - 1.0) * 100.0) if dirn == "LONG" else ((1.0 - _pk / _ep) * 100.0)
                        else:
                            _peak_pnl = 0.0
                        # 현재 미실현 손익% (캐시 현재가 — 부모님 "손실 정도", 2026-06-07)
                        _cur = 0.0
                        try:
                            _cur = float(fm._get_current_price(sym) or 0.0)
                        except Exception:  # noqa: BLE001
                            _cur = 0.0
                        if _ep > 0 and _cur > 0:
                            _pnl = ((_cur / _ep - 1.0) * 100.0) if dirn == "LONG" else ((1.0 - _cur / _ep) * 100.0)
                        else:
                            _pnl = 0.0
                        # 순손익 USDT — notional×% − 왕복 0.11% (Positions 패널 Net 과 동일 공식)
                        _qty = float(getattr(pos, "qty", 0.0) or 0.0)
                        _notional = _ep * _qty
                        _pnl_usdt = _notional * (_pnl / 100.0) - _notional * 0.0011 if _notional > 0 else 0.0
                        active.append(PeerActivePosition(
                            symbol=sym, direction=dirn,
                            age_min=round(_age, 1), peak_pnl_pct=round(_peak_pnl, 2),
                            pnl_pct=round(_pnl, 2), pnl_usdt=round(_pnl_usdt, 2),
                        ))
                health["focus_active"] = len(active)
                # 점수 합격인데 막판 차단된 near-miss (메모리 링버퍼, 저널 파싱 X — 2026-06-07 부모)
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
    """청산 사유가 '성공' 패턴인지 — TP / trailing / BE 도달 등."""
    r = (reason or "").lower()
    if not r:
        return False
    return any(k in r for k in ("tp1", "tp2", "tp_", "trail", "be_", "breakeven", "profit"))


# ── Polling (백그라운드) ──
async def _fetch_one(url: str, timeout: float) -> Optional[PeerBrief]:
    """단일 옆 서버 polling. 자기 자신 응답 detect 시 None."""
    headers = {}
    tok = get_peer_token()
    if tok:
        headers["X-Peer-Token"] = tok
    # Cloudflare Access Service Token (옆 서버가 Access 로 보호되면 통과)
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
        # 자기 self-polling detect — brief.server_id 가 자기 ID 와 같으면 cache 차단
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
    """백그라운드 polling task — main.py lifespan 에서 시작."""
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
                            # 만료된 캐시는 삭제 — stale brief 로 차단 X
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
    """main.py lifespan startup 에서 호출."""
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


# ── Gate (sync, focus_manager 호출) ──
def check_peer_block(symbol: str, direction: str) -> Tuple[bool, str]:
    """옆 서버가 같은 코인+방향 최근 SL 또는 현재 보유 시 (True, reason).

    리턴:
        (True, reason)  → 차단 (실제 reject)
        (False, "")     → 통과
        (False, "paper:...")  → paper mode = 매칭 있지만 차단 안 함 (로그용)

    enabled=False → 항상 (False, "")
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

    # 스냅샷 (lock 없이 dict 복제) — race condition 무해 (옆 polling 은 별도 task)
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
    """옆 서버 양의 신호 — 같은 코인+방향 옆 서버 최근 N분 win 매칭 시 (bonus, source).

    리턴:
        (0.0, "")        — 매칭 없음 / 비활성
        (bonus, source)  — 가점 + 출처 (예: "BiBit_Home BNBUSDT/LONG 3m ago TP1")

    가점은 *한 번만* (가장 가까운 win). 여러 옆 서버 매칭돼도 중복 가산 X.
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
    """옆 서버 같은 코인+방향 최근 SL 매칭 시 conviction(guard_score) 감점. 0 = 비활성.
    ⚠ 하드차단 아님 — 본체 지표(현재 반등 포함)가 강하면 이 감점을 넘어 진입한다.
    부모님 2026-06-06: "옆은 자체 로직 진입 전 한 번 더 생각하는 참고 차원."."""
    v = _STATE.get("peer_sl_penalty")
    if isinstance(v, (int, float)):
        return max(0.0, min(50.0, float(v)))
    return max(0.0, min(50.0, env_float("PEER_BRIEF_SL_PENALTY", 8.0)))


def check_peer_sl_caution(symbol: str, direction: str) -> Tuple[float, str]:
    """옆 서버 같은 코인+방향 최근 SL(손실 청산) → (penalty, source). 소프트 감점용.

    리턴:
        (0.0, "")        — 매칭 없음 / 비활성
        (penalty, source)
    ⚠ 하드차단 아님 (옛 check_peer_block 폐기). '보유'는 무시하고 최근 SL 만 본다.
    "SL 자리 무변화 재진입" 은 본체 가드(Reentry/dir_fail_window) + 이 감점으로 걸러지고,
    "지표 반등" 이면 본체 conviction 이 높아 감점을 넘으므로 부모님 "반등 가점" 이 자연 처리됨.
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

    best: Optional[Tuple[str, PeerLossExit]] = None  # (server_id, loss) — 가장 최근
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
    """옆이 같은 코인+방향을 N분+ 들고도 한 번도 제대로 green 못 친(헤맴/오실레이션) 자리면 conviction 감점.
    부모님 2026-06-06 TON 사례: 한 서버가 진입 후 방향 바뀜·5~10분 엎치락뒤치락 중인데 다른 서버가
    같은 방향 또 진입하는 것 방지. ⚠ 하드차단 X, soft nudge — 건강한 수익 라이딩은 안 건드림."""
    v = _STATE.get("peer_struggle_penalty")
    if isinstance(v, (int, float)):
        return max(0.0, min(50.0, float(v)))
    return max(0.0, min(50.0, env_float("PEER_BRIEF_STRUGGLE_PENALTY", 8.0)))


def get_peer_struggle_age_min() -> float:
    """고전 판정 — 보유 N분 이상 (runtime _STATE > env). UI 조절용."""
    v = _STATE.get("peer_struggle_age_min")
    if isinstance(v, (int, float)) and v > 0:
        return max(1.0, float(v))
    return max(1.0, env_float("PEER_BRIEF_STRUGGLE_AGE_MIN", 5.0))


def get_peer_struggle_peak_pct() -> float:
    """고전 판정 — 보유 중 최고 도달 수익% 가 이 미만이면 '못 green'(헤맴). (runtime _STATE > env). UI 조절용."""
    v = _STATE.get("peer_struggle_peak_pct")
    if isinstance(v, (int, float)):
        return float(v)
    return env_float("PEER_BRIEF_STRUGGLE_PEAK_PCT", 0.3)


def check_peer_struggle_caution(symbol: str, direction: str) -> Tuple[float, str]:
    """옆이 같은 코인+방향을 '고전 중 보유'(age ≥ N분 AND peak_pnl < 임계 = 한 번도 제대로 못 green)
    → (penalty, source). 수익 라이딩(peak_pnl 높음)이나 갓 진입(age 짧음)은 감점 안 함.

    age/peak_pnl 은 brief 의 PeerActivePosition 에 실려옴(현재가 조회 불필요). 옛 brief(필드 0)는
    age_min=0 이라 자동 제외 → 하위호환 안전.
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
    peak_max = get_peer_struggle_peak_pct()  # 이 %↑ green 찍었으면 건강 → 제외

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
    """옆이 '반대 방향'을 건강하게(수익권 도달) 보유 중인데 내가 그 반대로 진입하려 할 때 conviction 감점.
    부모님 2026-06-07 자가충돌(ZEC Home LONG ↔ server-c SHORT): 반대 보유는 헤지라 손실은 줄지만
    수수료 먹고 이익 0 → 신중해야. ⚠ 단, 상대가 '헤맴'(peak 낮음 = 방향 꺾이는 중)이면 감점 X —
    그땐 내 반대 진입이 '전환 포착'(레짐 flip)이라 오히려 맞을 수 있음. 하드차단 X, soft nudge. 0=비활성.
    runtime _STATE > env. (UI 조절용)"""
    v = _STATE.get("peer_conflict_penalty")
    if isinstance(v, (int, float)):
        return max(0.0, min(50.0, float(v)))
    return max(0.0, min(50.0, env_float("PEER_BRIEF_CONFLICT_PENALTY", 8.0)))


def check_peer_conflict_caution(symbol: str, direction: str) -> Tuple[float, str]:
    """옆이 '반대 방향'을 건강하게(peak ≥ 임계 = 수익권 도달) 보유 중인데 내가 그 반대로
    진입 → (penalty, source). 자가충돌(자본 상쇄·수수료로 이익0) 신중용. soft 감점.

    ★ 비대칭 (부모님 2026-06-07): 상대가 '헤맴'(peak < 임계 = 한 번도 제대로 green 못 침)이면 감점 0 —
      상대 방향이 꺾이는 중 → 내 반대 진입은 '전환 포착'이므로 막지 않는다([[feedback_regime_flip_asymmetric]]).
    건강 판정 임계 = get_peer_struggle_peak_pct() 재사용 (struggle 과 같은 기준선, 노브 1개로 통일).
    peak_pnl_pct 는 brief 의 PeerActivePosition 에 실려옴. 옛 brief(필드 0)는 peak 0 < 임계 → 자동 제외(안전).
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

    peak_min = get_peer_struggle_peak_pct()  # 상대 peak 이 이 %↑ = 건강(수익권) → 충돌 신중 / 미만 = 헤맴 → 통과

    best: Optional[Tuple[str, PeerActivePosition]] = None  # 가장 건강한(peak 큰) 반대 포지션
    for url, brief in list(_BRIEF_CACHE.items()):
        if brief is None:
            continue
        for ap in brief.active_positions:
            if ap.symbol != sym or ap.direction != opp:
                continue
            if ap.peak_pnl_pct < peak_min:
                continue  # 상대가 헤맴 = 전환 신호 → 감점 X (turn-catch)
            if best is None or ap.peak_pnl_pct > best[1].peak_pnl_pct:
                best = (brief.server_id or url, ap)

    if best is None:
        return 0.0, ""
    peer_id, ap = best
    src = f"{peer_id} holds {sym}/{opp} peak{ap.peak_pnl_pct:+.2f}% (vs my {dirn})"
    return penalty, src


# ── 🌊 [2026-06-15 부모] 함대 몰림(crowding) — 옆이 같은 코인+'같은' 방향 보유 시 soft 감점 ──
#   오늘 TRUMP LONG: 4서버가 4분 안에 거의 동시 진입(341~345분 전) → 분산 깨짐 → 골고루 −3.5%.
#   기존 struggle 감점은 'age≥5분 AND peak<0.3%'(헤맴) 조건이라 갓 진입한 건강 보유는 안 셈 →
#   동시 진입 구멍. 부모님 "옆 SL뿐 아니라 *현재 보유현황*도 참조하라" → 옆이 지금 같은 방향
#   들고 있으면 그 자체가 몰림 신호. ⚠ 하드차단 X(부모님 "보고도 들어가야"), soft 감점만 —
#   본체 conviction 강하면 넘어 진입. graduated(옆 1서버당 N점, cap 까지). default OFF(opt-in).
def get_peer_crowding_penalty() -> float:
    """옆 서버 1대당 같은 코인+같은 방향 보유 시 conviction 감점(graduated). 0 = 비활성(default)."""
    v = _STATE.get("peer_crowding_penalty")
    if isinstance(v, (int, float)):
        return max(0.0, min(50.0, float(v)))
    return max(0.0, min(50.0, env_float("PEER_BRIEF_CROWDING_PENALTY", 0.0)))  # ★ default OFF


def get_peer_crowding_cap() -> float:
    """몰림 감점 총 상한(점). 여러 서버 몰려도 이 이상은 안 깎음."""
    v = _STATE.get("peer_crowding_cap")
    if isinstance(v, (int, float)) and v > 0:
        return max(1.0, min(50.0, float(v)))
    return max(1.0, min(50.0, env_float("PEER_BRIEF_CROWDING_CAP", 12.0)))


def check_peer_crowding_caution(symbol: str, direction: str) -> Tuple[float, str]:
    """옆 서버들이 같은 코인+'같은' 방향을 *현재 보유 중*인 서버 수 → (penalty, source). soft 감점.

    struggle/conflict 와 달리 age·peak 조건 없음 — '갓 진입한 건강 보유'도 몰림으로 셈(동시 진입 차단).
    penalty = min(cap, 보유서버수 × per). 옆 캐시 비면 0 = fail-open. 같은 방향만(반대는 conflict 담당).
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
    src = f"함대 {sym}/{dirn} 옆 {n}서버 보유 몰림 (서버당 −{per:.0f}, cap {get_peer_crowding_cap():.0f})"
    return penalty, src


# ── 🛡️ [2026-06-11 부모] 함대 dir_fail — 같은 코인·방향 손실 함대 누적 차단 ──
#   오늘 VELVET LONG: server-b 8:59→server-a 9:43→home 9:48 순차(50분 시간차) 줄초상.
#   기존 check_peer_sl_caution(soft 8점·30분)은 ① 윈도우 30분<시간차49분 ② soft 가 강신호 못 막음 → 실패.
#   부모님 "손실 기록 서로 확인 못 해 진입 자유로웠다" → 함대(옆+나) 같은 코인+방향 누적 N회 → 하드 차단.
def get_peer_fleet_dirfail_enabled() -> bool:
    v = _STATE.get("fleet_dirfail_enabled")
    if isinstance(v, bool):
        return v
    return env_bool("PEER_BRIEF_FLEET_DIRFAIL_ENABLED", default=False)  # ★ default OFF


def get_peer_fleet_dirfail_max() -> int:
    """함대 누적 손실 N회 이상 = 차단 (옆+나 합산). default 2 = 첫 시도 허용·2번째부터 막음."""
    v = _STATE.get("fleet_dirfail_max")
    if isinstance(v, (int, float)) and v > 0:
        return max(1, int(v))
    return max(1, env_int("PEER_BRIEF_FLEET_DIRFAIL_MAX", default=2))


def get_peer_fleet_dirfail_window_min() -> int:
    """함대 손실 누적 윈도우(분) — 로컬 dir_fail 4h(240분)와 통일. 시간차 손실 커버."""
    v = _STATE.get("fleet_dirfail_window_min")
    if isinstance(v, (int, float)) and v > 0:
        return max(1, int(v))
    return max(1, env_int("PEER_BRIEF_FLEET_DIRFAIL_WINDOW_MIN", default=240))


def check_peer_direction_fail_caution(symbol: str, direction: str, local_count: int = 0) -> Tuple[bool, str]:
    """함대(옆 서버들 + 나) 같은 코인+방향 누적 손실 ≥ max → (blocked, source). 하드 차단용.

    local_count = 내 저널의 같은 코인+방향 윈도우내 손실 수 (focus_manager 에서 계산해 전달).
    peer_count = 모든 옆 brief.recent_losses 중 같은 코인+방향+윈도우내 손실 수.
    합산 ≥ max → 진입 차단. paper/dry 면 차단 안 함(로그만). _BRIEF_CACHE 비면 peer 0 = fail-open.
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
    src = (f"함대 {sym}/{dirn} {total}실패≥{fail_max} "
           f"(peer {peer_count}/{len(servers)}서버 + 로컬 {local_count}, {win_h:.0f}h 윈도우)")
    if is_paper():
        return False, f"paper:{src}"
    return True, src


def get_cache_snapshot() -> dict:
    """대시보드/디버그용 — 현재 캐시 상태."""
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
            # ── 상세 (Peer Brief Scanner, 2026-06-07 부모) — 캐시에 이미 있는 것 노출 ──
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
