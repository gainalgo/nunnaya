# ============================================================
# Twin Battle — 형(兄)↔동생(弟) 서버 성과 비교 시스템
# ------------------------------------------------------------
# 형 서버가 동생 서버에게 주는 선물.
# 동일한 FOCUS 전략을 돌리는 두 서버가 서로의 성과를
# 표준 포맷으로 내보내고, 대결 결과를 비교한다.
#
# READ-ONLY: 거래 행동에는 절대 개입하지 않는다.
#
# 사용:
#   from app.manager.twin_battle import twin_battle
#   snap = twin_battle.export_snapshot()
#   result = twin_battle.compare(other_server_snapshot)
# ============================================================
from __future__ import annotations

import json
import logging
import os
import time
import datetime as _dt
from collections import defaultdict
from typing import Any, Dict, List, Optional

from app.core.io_utils import safe_write_json, safe_load_json

logger = logging.getLogger(__name__)

# ── 경로 ─────────────────────────────────────────────────────
JOURNAL_PATH = os.path.join("runtime", "focus_harpoon_journal.jsonl")
CONFIG_PATH = os.path.join("runtime", "focus_config.json")
TWIN_CONFIG_PATH = os.path.join("runtime", "twin_config.json")
TWIN_SNAPSHOT_DIR = os.path.join("runtime", "twin_snapshots")

# ── 07:00 KST = 22:00 UTC 리셋 기준 ─────────────────────────
_RESET_HOUR_UTC = 22


def _today_reset_ts() -> float:
    """오늘 07:00 KST (22:00 UTC) 리셋 타임스탬프 반환."""
    now_utc = _dt.datetime.now(_dt.timezone.utc)
    boundary = now_utc.replace(hour=_RESET_HOUR_UTC, minute=0, second=0, microsecond=0)
    if now_utc.hour < _RESET_HOUR_UTC:
        boundary -= _dt.timedelta(days=1)
    return boundary.timestamp()


def _today_date_label() -> str:
    """오늘 거래일 라벨 (KST 기준 YYYY-MM-DD)."""
    now_utc = _dt.datetime.now(_dt.timezone.utc)
    boundary = now_utc.replace(hour=_RESET_HOUR_UTC, minute=0, second=0, microsecond=0)
    if now_utc.hour < _RESET_HOUR_UTC:
        boundary -= _dt.timedelta(days=1)
    kst = boundary + _dt.timedelta(hours=9)
    return kst.strftime("%Y-%m-%d")


# ── Helper: 저널 읽기 ────────────────────────────────────────

def _read_all_exits() -> List[Dict]:
    """저널에서 모든 EXIT 거래 로드."""
    exits: List[Dict] = []
    if not os.path.exists(JOURNAL_PATH):
        return exits
    try:
        with open(JOURNAL_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    if rec.get("event") == "EXIT":
                        exits.append(rec)
                except json.JSONDecodeError:
                    continue
    except OSError as exc:
        logger.warning("[TwinBattle] 저널 읽기 실패: %s", exc)
    return exits


def _read_today_exits() -> List[Dict]:
    """오늘 07:00 KST 이후 EXIT 거래만 필터."""
    reset_ts = _today_reset_ts()
    return [t for t in _read_all_exits() if t.get("ts", 0) >= reset_ts]


def _read_today_blocked() -> List[Dict]:
    """오늘 BLOCKED 이벤트 로드."""
    reset_ts = _today_reset_ts()
    blocked: List[Dict] = []
    if not os.path.exists(JOURNAL_PATH):
        return blocked
    try:
        with open(JOURNAL_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    if rec.get("event") == "BLOCKED" and rec.get("ts", 0) >= reset_ts:
                        blocked.append(rec)
                except json.JSONDecodeError:
                    continue
    except OSError as exc:
        logger.warning("[TwinBattle] BLOCKED 읽기 실패: %s", exc)
    return blocked


def _read_active_positions() -> List[Dict]:
    """focus_config.json에서 현재 보유 포지션 읽기."""
    positions: List[Dict] = []
    data = safe_load_json(CONFIG_PATH, default={})
    # ★ Phase H (2026-04-20 형 letter#3 A-11): isinstance 가드 — 외부 도구로 수정 시 type 깨질 수 있음
    if not isinstance(data, dict):
        return positions
    state = data.get("state", {})
    if not isinstance(state, dict):
        return positions

    # multi-position 리스트
    raw_positions = state.get("positions", [])
    if not isinstance(raw_positions, list):
        raw_positions = []
    for p in raw_positions:
        if p.get("market") and p.get("entry_price", 0) > 0:
            positions.append({
                "coin": p.get("market", ""),
                "direction": p.get("direction", ""),
                "entry_price": p.get("entry_price", 0.0),
                "current_pnl_pct": 0.0,  # 실시간 가격 없이 placeholder
                "hold_min": round((time.time() - p.get("entry_ts", time.time())) / 60, 1),
                "conviction": p.get("conviction_score", 0),
                "leverage": p.get("leverage", 1),
                "qty": p.get("qty", 0.0),
            })

    # Legacy single position 호환
    legacy = state.get("position")
    if legacy and legacy.get("market") and legacy.get("entry_price", 0) > 0:
        already = {p["coin"] for p in positions}
        if legacy["market"] not in already:
            positions.append({
                "coin": legacy.get("market", ""),
                "direction": legacy.get("direction", ""),
                "entry_price": legacy.get("entry_price", 0.0),
                "current_pnl_pct": 0.0,
                "hold_min": round((time.time() - legacy.get("entry_ts", time.time())) / 60, 1),
                "conviction": legacy.get("conviction_score", 0),
                "leverage": legacy.get("leverage", 1),
                "qty": legacy.get("qty", 0.0),
            })

    return positions


def _calc_today_coin_summary(exits: List[Dict]) -> Dict[str, Dict]:
    """코인별 오늘 성과 요약."""
    by_coin: Dict[str, Dict] = defaultdict(lambda: {
        "trades": 0, "pnl": 0.0, "wins": 0,
    })
    for t in exits:
        coin = t.get("market", "UNKNOWN")
        pnl = t.get("pnl_net", 0) or 0
        by_coin[coin]["trades"] += 1
        by_coin[coin]["pnl"] = round(by_coin[coin]["pnl"] + pnl, 4)
        if pnl > 0:
            by_coin[coin]["wins"] += 1

    result = {}
    for coin, data in sorted(by_coin.items(), key=lambda x: x[1]["pnl"], reverse=True):
        wr = round(data["wins"] / data["trades"] * 100, 1) if data["trades"] > 0 else 0.0
        result[coin] = {
            "trades": data["trades"],
            "pnl": data["pnl"],
            "win_rate": wr,
        }
    return result


def _calc_blocked_counts(blocked_entries: List[Dict]) -> Dict[str, int]:
    """BLOCKED 이벤트를 사유별로 카운트."""
    counts: Dict[str, int] = defaultdict(int)
    for entry in blocked_entries:
        reason = entry.get("exit_reason", "unknown")
        # 간략화: 세부 내용 제거
        key = reason.split(":")[0] if ":" in reason else reason
        counts[key] += 1
    return dict(counts)


def _calc_exposure(positions: List[Dict]) -> float:
    """총 노출 금액 (notional value) 계산."""
    total = 0.0
    for p in positions:
        price = p.get("entry_price", 0)
        qty = p.get("qty", 0)
        total += price * qty
    return round(total, 2)


# ── 대결 메시지 생성 ─────────────────────────────────────────

_RIVALRY_MESSAGES = {
    "dominant": [
        "{winner}이(가) 압도적! {loser} 분발해라~",
        "{winner} 완승! {loser}은(는) 오늘 반성문 써라",
        "오늘은 {winner}의 날. {loser}은(는) 조용히 공부해",
    ],
    "close": [
        "엎치락뒤치락! {winner}이(가) 간신히 앞서는 중",
        "거의 동점! 아직 모른다, {loser} 역전 가능",
        "치열한 접전 — {winner}이(가) 살짝 리드",
    ],
    "tied": [
        "쌍둥이답게 완전 동점! 진짜 넌나야~",
        "형제의 기운이 하나로... 수익도 같다",
        "DNA가 같으니 성과도 같나보다",
    ],
    "no_data": [
        "아직 비교할 데이터가 없어. 열심히 거래하자!",
        "데이터 모이면 그때 승부!",
    ],
}


def _pick_message(category: str, **kwargs) -> str:
    """카테고리에서 랜덤(시간 기반) 메시지 선택."""
    messages = _RIVALRY_MESSAGES.get(category, _RIVALRY_MESSAGES["no_data"])
    # 시간 기반 순환 (deterministic)
    idx = int(time.time() / 3600) % len(messages)
    return messages[idx].format(**kwargs)


def _find_biggest_diff_coin(coin_battles: List[Dict]) -> str:
    """코인 대결에서 가장 큰 PnL 차이 코인 찾기."""
    if not coin_battles:
        return ""
    best = max(coin_battles, key=lambda x: abs(x.get("diff", 0)))
    return best.get("coin", "")


# ── TwinBattle 메인 클래스 ───────────────────────────────────

class TwinBattle:
    """형↔동생 서버 성과 비교 매니저.

    READ-ONLY 시스템: 거래에 절대 간섭하지 않는다.
    export_snapshot()으로 표준 데이터를 내보내고,
    compare()로 상대방 데이터와 비교한다.
    """

    def __init__(self, server_name: str = "server-a"):
        # twin_config.json에서 이름 로드 (없으면 기본값)
        cfg = safe_load_json(TWIN_CONFIG_PATH, default={})
        self._server_name: str = cfg.get("server_name", server_name)
        self._last_export_ts: float = 0.0
        self._boot_ts: float = time.time()
        os.makedirs(TWIN_SNAPSHOT_DIR, exist_ok=True)
        logger.info("[TwinBattle] 초기화: server_name=%s", self._server_name)

    # ── Server Name ──────────────────────────────────────────

    def set_server_name(self, name: str):
        """서버 이름 변경 및 저장."""
        self._server_name = name
        cfg = safe_load_json(TWIN_CONFIG_PATH, default={})
        cfg["server_name"] = name
        safe_write_json(TWIN_CONFIG_PATH, cfg)
        logger.info("[TwinBattle] 서버 이름 변경: %s", name)

    def get_status(self) -> Dict[str, Any]:
        """현재 상태 반환."""
        return {
            "server_name": self._server_name,
            "last_export_ts": self._last_export_ts,
            "last_export_ago_sec": round(time.time() - self._last_export_ts, 1) if self._last_export_ts > 0 else None,
            "uptime_sec": round(time.time() - self._boot_ts, 1),
        }

    # ── Export Snapshot ──────────────────────────────────────

    def export_snapshot(self) -> Dict[str, Any]:
        """표준 성과 스냅샷 내보내기 — 쌍둥이 비교의 핵심 함수.

        Returns:
            표준화된 성과 dict (다른 서버에서 compare()에 전달 가능)
        """
        now = time.time()
        all_exits = _read_all_exits()
        today_exits = []
        reset_ts = _today_reset_ts()
        for t in all_exits:
            if t.get("ts", 0) >= reset_ts:
                today_exits.append(t)

        today_blocked = _read_today_blocked()
        active_positions = _read_active_positions()

        # ── Overall Stats ──
        total_pnl = sum(t.get("pnl_net", 0) or 0 for t in all_exits)
        total_wins = sum(1 for t in all_exits if (t.get("pnl_net", 0) or 0) > 0)
        overall_wr = round(total_wins / len(all_exits) * 100, 1) if all_exits else 0.0

        # ── Today Stats ──
        today_pnl = sum(t.get("pnl_net", 0) or 0 for t in today_exits)
        today_wins = sum(1 for t in today_exits if (t.get("pnl_net", 0) or 0) > 0)
        today_wr = round(today_wins / len(today_exits) * 100, 1) if today_exits else 0.0

        # 금액 기준 승률 (amt_win_rate)
        win_pnl = sum(t.get("pnl_net", 0) or 0 for t in all_exits if (t.get("pnl_net", 0) or 0) > 0)
        loss_pnl = abs(sum(t.get("pnl_net", 0) or 0 for t in all_exits if (t.get("pnl_net", 0) or 0) <= 0))
        amt_wr = round(win_pnl / (win_pnl + loss_pnl) * 100, 1) if (win_pnl + loss_pnl) > 0 else 0.0

        # ── Today Best/Worst ──
        today_best = _find_best_trade(today_exits)
        today_worst = _find_worst_trade(today_exits)

        # ── Coin Summary ──
        coin_summary = _calc_today_coin_summary(today_exits)

        # ── Blocked Counts ──
        blocked_counts = _calc_blocked_counts(today_blocked)

        # ── Daily Plans / SL Count (focus_config에서 읽기) ──
        focus_data = safe_load_json(CONFIG_PATH, default={})
        focus_state = focus_data.get("state", {})
        daily_plans_used = focus_state.get("daily_plans_used", 0)
        daily_sl_count = focus_state.get("daily_sl_count", 0)

        # ── Exposure ──
        total_exposure = _calc_exposure(active_positions)

        snapshot = {
            "server_name": self._server_name,
            "export_ts": now,
            "export_date": _today_date_label(),
            # === Overall Stats ===
            "total_pnl": round(total_pnl, 4),
            "today_pnl": round(today_pnl, 4),
            "total_trades": len(all_exits),
            "today_trades": len(today_exits),
            "overall_win_rate": overall_wr,
            "today_win_rate": today_wr,
            "amt_win_rate": amt_wr,
            # === Active Positions ===
            "active_positions": active_positions,
            "total_exposure": total_exposure,
            # === Today's Best/Worst ===
            "today_best_trade": today_best,
            "today_worst_trade": today_worst,
            # === Coin Overlap ===
            "coin_summary_today": coin_summary,
            # === Timing Analysis ===
            "avg_entry_delay_sec": 0.0,  # placeholder — 추후 cross-server 비교용
            # === System Health ===
            "uptime_sec": round(now - self._boot_ts, 1),
            "blocked_today": blocked_counts,
            "daily_plans_used": daily_plans_used,
            "daily_sl_count": daily_sl_count,
        }

        self._last_export_ts = now
        self._save_snapshot(snapshot)
        return snapshot

    # ── Compare ──────────────────────────────────────────────

    def compare(self, other_snapshot: Dict) -> Dict[str, Any]:
        """이 서버 vs 상대 서버 성과 비교.

        Args:
            other_snapshot: 상대 서버의 export_snapshot() 결과

        Returns:
            대결 결과 dict — PnL 차이, 코인별 승부, 요약 메시지 포함
        """
        my_snap = self.export_snapshot()
        my_name = my_snap.get("server_name", "server-a")
        other_name = other_snapshot.get("server_name", "server-b")

        # ── PnL 비교 ──
        my_today_pnl = my_snap.get("today_pnl", 0)
        other_today_pnl = other_snapshot.get("today_pnl", 0)
        pnl_diff = round(my_today_pnl - other_today_pnl, 4)

        my_total_pnl = my_snap.get("total_pnl", 0)
        other_total_pnl = other_snapshot.get("total_pnl", 0)
        total_diff = round(my_total_pnl - other_total_pnl, 4)

        winner_today = my_name if pnl_diff > 0 else (other_name if pnl_diff < 0 else "무승부")
        winner_total = my_name if total_diff > 0 else (other_name if total_diff < 0 else "무승부")

        # ── 승률 비교 ──
        wr_diff = round(
            (my_snap.get("today_win_rate", 0) or 0) - (other_snapshot.get("today_win_rate", 0) or 0),
            1,
        )

        # ── 코인별 대결 (양쪽 모두 거래한 코인) ──
        my_coins = my_snap.get("coin_summary_today", {})
        other_coins = other_snapshot.get("coin_summary_today", {})
        overlap_coins = set(my_coins.keys()) & set(other_coins.keys())

        coin_battles: List[Dict] = []
        my_coin_wins = 0
        other_coin_wins = 0

        for coin in sorted(overlap_coins):
            my_coin_pnl = my_coins[coin].get("pnl", 0)
            other_coin_pnl = other_coins[coin].get("pnl", 0)
            diff = round(my_coin_pnl - other_coin_pnl, 4)

            if my_coin_pnl > other_coin_pnl:
                coin_winner = my_name
                my_coin_wins += 1
            elif other_coin_pnl > my_coin_pnl:
                coin_winner = other_name
                other_coin_wins += 1
            else:
                coin_winner = "무승부"

            battle = {
                "coin": coin,
                my_name + "_pnl": round(my_coin_pnl, 4),
                other_name + "_pnl": round(other_coin_pnl, 4),
                "diff": diff,
                "winner": coin_winner,
            }
            coin_battles.append(battle)

        coin_battle_score = f"{my_name} {my_coin_wins} : {other_name} {other_coin_wins}"

        # ── 거래 수 비교 ──
        my_today_trades = my_snap.get("today_trades", 0)
        other_today_trades = other_snapshot.get("today_trades", 0)

        # ── 요약 메시지 생성 (형제간 라이벌리!) ──
        summary = _build_summary_message(
            my_name=my_name,
            other_name=other_name,
            pnl_diff=pnl_diff,
            my_coin_wins=my_coin_wins,
            other_coin_wins=other_coin_wins,
            coin_battles=coin_battles,
            my_today_trades=my_today_trades,
            other_today_trades=other_today_trades,
        )

        label = f"{my_name}_vs_{other_name}"
        return {
            label: {
                "pnl_diff": pnl_diff,
                "total_pnl_diff": total_diff,
                "winner_today": winner_today,
                "winner_total": winner_total,
                "win_rate_diff": wr_diff,
                # 코인 대결
                "coin_battles": coin_battles,
                "coin_battle_score": coin_battle_score,
                # 거래 수
                f"{my_name}_trades": my_today_trades,
                f"{other_name}_trades": other_today_trades,
                # 요약
                "summary": summary,
            }
        }

    # ── Private ──────────────────────────────────────────────

    def _save_snapshot(self, snapshot: Dict):
        """스냅샷을 twin_snapshots/ 에 저장 (추세 비교용)."""
        try:
            date_str = snapshot.get("export_date", "unknown")
            ts_suffix = int(snapshot.get("export_ts", time.time()))
            path = os.path.join(TWIN_SNAPSHOT_DIR, f"{date_str}_{ts_suffix}.json")
            safe_write_json(path, snapshot)
        except Exception as exc:
            logger.warning("[TwinBattle] 스냅샷 저장 실패: %s", exc)


# ── Private Helpers (모듈 레벨) ──────────────────────────────

def _find_best_trade(exits: List[Dict]) -> Dict:
    """가장 좋은 거래 찾기."""
    if not exits:
        return {}
    best = max(exits, key=lambda t: t.get("pnl_net", 0) or 0)
    pnl = best.get("pnl_net", 0) or 0
    if pnl <= 0:
        return {}
    return {
        "coin": best.get("market", ""),
        "pnl": round(pnl, 4),
        "roe_pct": round(best.get("roe_pct", 0) or 0, 2),
    }


def _find_worst_trade(exits: List[Dict]) -> Dict:
    """가장 나쁜 거래 찾기."""
    if not exits:
        return {}
    worst = min(exits, key=lambda t: t.get("pnl_net", 0) or 0)
    pnl = worst.get("pnl_net", 0) or 0
    if pnl >= 0:
        return {}
    return {
        "coin": worst.get("market", ""),
        "pnl": round(pnl, 4),
        "roe_pct": round(worst.get("roe_pct", 0) or 0, 2),
    }


def _build_summary_message(
    *,
    my_name: str,
    other_name: str,
    pnl_diff: float,
    my_coin_wins: int,
    other_coin_wins: int,
    coin_battles: List[Dict],
    my_today_trades: int,
    other_today_trades: int,
) -> str:
    """형제 대결 요약 메시지 생성 (한국어, 재미있게!)."""

    # 데이터 없음
    if my_today_trades == 0 and other_today_trades == 0:
        return _pick_message("no_data")

    # 상대방 데이터 없음
    if other_today_trades == 0:
        return f"{my_name}만 거래 중 (${abs(pnl_diff):+.2f}). {other_name}은(는) 아직 출발 전!"

    # 내 데이터 없음
    if my_today_trades == 0:
        return f"{other_name}만 거래 중. {my_name}은(는) 아직 출발 전!"

    # 승자/패자 결정
    abs_diff = abs(pnl_diff)
    if abs_diff < 0.01:
        # 동점
        msg = _pick_message("tied")
    else:
        winner = my_name if pnl_diff > 0 else other_name
        loser = other_name if pnl_diff > 0 else my_name

        if abs_diff > 50:
            msg = _pick_message("dominant", winner=winner, loser=loser)
        else:
            msg = _pick_message("close", winner=winner, loser=loser)

    # 코인 대결 스코어 추가
    if coin_battles:
        biggest_coin = _find_biggest_diff_coin(coin_battles)
        if biggest_coin:
            msg += f" {biggest_coin}에서 가장 큰 차이."

    # PnL 수치 추가
    if abs_diff >= 0.01:
        winner = my_name if pnl_diff > 0 else other_name
        msg = f"{winner}이(가) +${abs_diff:.2f} 앞서는 중. 코인 대결 {my_coin_wins}:{other_coin_wins}. {msg}"
    else:
        msg = f"PnL 동점! 코인 대결 {my_coin_wins}:{other_coin_wins}. {msg}"

    return msg


# ── Singleton ────────────────────────────────────────────────
twin_battle = TwinBattle()
