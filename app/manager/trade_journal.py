# ============================================================
# Trade Journal — FOCUS + Harpoon 전용 거래 장부
# ------------------------------------------------------------
# JSONL 파일로 모든 진입/청산을 기록.
# 각 레코드에 전략명, 방향, 가격, 수량, PnL, 수수료, 이유 포함.
#
# 사용:
#   from app.manager.trade_journal import journal
#   journal.record_entry(...)
#   journal.record_exit(...)
#   journal.get_trades(limit=50)
#   journal.get_summary()
# ============================================================
from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

JOURNAL_PATH = os.path.join("runtime", "focus_harpoon_journal.jsonl")

# Bybit taker fee (one-way)
FEE_RATE = 0.00055

# ── EXIT dedup guard ──
# phantom EXIT 경로(_check_bybit_positions) 와 SYNC 경로(_live_sync) 가 같은 SERVER_SL 청산을
# 각각 기록하는 버그(2026-04-23 BCHUSDT 3중 기록 관측) 를 방지.
# 동일 (market, direction, entry_price, reason) 이 윈도우 내 재호출되면 skip.
# cooldown_sec(기본 600s) 이 재진입/재청산을 막아주므로 30초 윈도우는 정상 거래를 가로막지 않음.
EXIT_DEDUP_WINDOW_SEC = 30.0
_EXIT_DEDUP_CACHE_CAP = 200


@dataclass
class TradeRecord:
    """단일 거래 기록."""
    ts: float = 0.0                 # 기록 시각 (unix timestamp)
    strategy: str = ""              # "FOCUS" or "HARPOON"
    event: str = ""                 # "ENTRY" / "EXIT" / "PARTIAL"
    market: str = ""
    direction: str = ""             # "LONG" or "SHORT"
    price: float = 0.0              # 체결 가격
    qty: float = 0.0
    # Exit 전용 필드
    entry_price: float = 0.0
    exit_reason: str = ""           # "TP1", "TP2", "SL", "TIMEOUT", "trend_reversal" 등
    pnl_gross: float = 0.0          # 수수료 전 PnL ($)
    fee: float = 0.0                # 수수료 ($)
    pnl_net: float = 0.0            # 순 PnL ($)
    pnl_pct: float = 0.0            # 수익률 (%)
    hold_sec: float = 0.0           # 보유 시간 (초)
    leverage: int = 1
    margin: float = 0.0             # 투입 마진 ($)
    roe_pct: float = 0.0            # Return on Equity (%)
    # 추가 정보
    phase: str = ""                 # FOCUS: SCOUT/REINFORCED/ALLIN
    dynamic_trailing: bool = False  # Dynamic Trailing 사용 여부
    breakeven_locked: bool = False  # 손익분기 잠금 여부
    peak_profit_pct: float = 0.0    # 최고 수익률 (%)


class TradeJournal:
    """FOCUS + Harpoon 전용 장부 매니저."""

    def __init__(self):
        self._lock = threading.Lock()
        self._recent_exits: Dict[tuple, float] = {}
        os.makedirs(os.path.dirname(JOURNAL_PATH), exist_ok=True)
        # ★ 인메모리 캐시 (2026-05-15 leak fix) ──
        # 매 5초 dashboard polling이 38MB 파일 풀파싱하던 누수 차단.
        # 첫 호출 시 한 번만 파일→list 로드. 이후 _append/get_*은 메모리만 사용.
        self._cache: List[Dict] = []
        self._cache_loaded = False

    def _ensure_cache(self):
        """★ 첫 호출 시 전체 파일 → 인메모리 list (1회만)."""
        if self._cache_loaded:
            return
        with self._lock:
            if self._cache_loaded:
                return
            try:
                if os.path.exists(JOURNAL_PATH):
                    with open(JOURNAL_PATH, "r", encoding="utf-8") as f:
                        for line in f:
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                self._cache.append(json.loads(line))
                            except json.JSONDecodeError:
                                continue
                logger.info("[JOURNAL] cache loaded: %d records", len(self._cache))
            except OSError as exc:
                logger.warning("[JOURNAL] cache load failed: %s", exc)
            self._cache_loaded = True

    def _append(self, record: TradeRecord):
        """JSONL 한 줄 추가 + 인메모리 캐시 갱신."""
        with self._lock:
            rec_dict = asdict(record)
            try:
                with open(JOURNAL_PATH, "a", encoding="utf-8") as f:
                    f.write(json.dumps(rec_dict, ensure_ascii=False) + "\n")
                    f.flush()
                    os.fsync(f.fileno())
            except OSError as exc:
                logger.warning("[JOURNAL] write failed: %s", exc)
            # ★ 캐시 갱신 (leak fix 2026-05-15) — 매번 풀파싱 차단의 핵심
            if self._cache_loaded:
                self._cache.append(rec_dict)

    # ── Record Blocked (차단된 진입 시도) ──

    def record_blocked(
        self,
        strategy: str,
        market: str,
        direction: str,
        reason: str,
        detail: str = "",
    ):
        """차단된 진입 시도 기록 — 나중에 '몇 번 막았는가' 분석용."""
        rec = TradeRecord(
            ts=time.time(),
            strategy=strategy.upper(),
            event="BLOCKED",
            market=market,
            direction=direction,
            exit_reason=reason,
            phase=detail,
        )
        self._append(rec)
        logger.info("[JOURNAL] %s BLOCKED %s %s | %s | %s",
                    strategy, direction, market, reason, detail)
        # ★ [2026-06-15 해결안 B] 게이트 집계 sink (옵션, FocusManager가 등록). 모든 BLOCKED의
        #   단일 funnel 이라 '왜 침묵했나' 완전 집계. 실패는 절대 기록 흐름에 전파 안 함.
        _sink = getattr(self, "_gate_sink", None)
        if _sink is not None:
            try:
                _sink(strategy, market, direction, reason)
            except Exception:  # noqa: BLE001
                pass

    # ── Record Entry ──

    def record_entry(
        self,
        strategy: str,
        market: str,
        direction: str,
        price: float,
        qty: float,
        leverage: int = 1,
        phase: str = "",
    ):
        """진입 기록."""
        margin = price * qty / leverage if leverage > 0 else price * qty
        rec = TradeRecord(
            ts=time.time(),
            strategy=strategy.upper(),
            event="ENTRY",
            market=market,
            direction=direction,
            price=price,
            qty=qty,
            leverage=leverage,
            margin=round(margin, 4),
            phase=phase,
        )
        self._append(rec)
        logger.info("[JOURNAL] %s ENTRY %s %s @ $%.2f qty=%.4f margin=$%.2f",
                    strategy, direction, market, price, qty, margin)

    # ── Record Exit ──

    def record_exit(
        self,
        strategy: str,
        market: str,
        direction: str,
        entry_price: float,
        exit_price: float,
        qty: float,
        reason: str,
        leverage: int = 1,
        hold_sec: float = 0.0,
        phase: str = "",
        dynamic_trailing: bool = False,
        breakeven_locked: bool = False,
        peak_profit_pct: float = 0.0,
    ):
        """청산 기록 (수수료 자동 계산)."""
        # ── Dedup 가드 — phantom/SYNC 경로 동시 발동 시 중복 기록 방지 ──
        _now = time.time()
        _dedup_key = (
            market.upper(),
            direction.upper(),
            round(float(entry_price), 8),
            reason,
        )
        with self._lock:
            _last_ts = self._recent_exits.get(_dedup_key)
            if _last_ts is not None and (_now - _last_ts) < EXIT_DEDUP_WINDOW_SEC:
                logger.warning(
                    "[JOURNAL] EXIT dedup: %s %s %s | %s (prev %.1fs ago) — skipped duplicate",
                    strategy.upper(), direction.upper(), market, reason, _now - _last_ts,
                )
                return
            self._recent_exits[_dedup_key] = _now
            # 캐시 정리 — 윈도우 ×10 이전 항목 제거
            if len(self._recent_exits) > _EXIT_DEDUP_CACHE_CAP:
                _cutoff = _now - EXIT_DEDUP_WINDOW_SEC * 10
                self._recent_exits = {
                    k: v for k, v in self._recent_exits.items() if v > _cutoff
                }

        # PnL 계산
        if direction.upper() == "LONG":
            pnl_gross = (exit_price - entry_price) * qty
        else:
            pnl_gross = (entry_price - exit_price) * qty

        fee = (entry_price * qty + exit_price * qty) * FEE_RATE
        pnl_net = pnl_gross - fee

        pnl_pct = ((exit_price / entry_price - 1) * 100) if direction.upper() == "LONG" else ((1 - exit_price / entry_price) * 100)

        margin = entry_price * qty / leverage if leverage > 0 else entry_price * qty
        roe_pct = (pnl_net / margin * 100) if margin > 0 else 0.0

        rec = TradeRecord(
            ts=time.time(),
            strategy=strategy.upper(),
            event="EXIT",
            market=market,
            direction=direction,
            price=exit_price,
            qty=qty,
            entry_price=entry_price,
            exit_reason=reason,
            pnl_gross=round(pnl_gross, 4),
            fee=round(fee, 4),
            pnl_net=round(pnl_net, 4),
            pnl_pct=round(pnl_pct, 4),
            hold_sec=round(hold_sec, 1),
            leverage=leverage,
            margin=round(margin, 4),
            roe_pct=round(roe_pct, 2),
            phase=phase,
            dynamic_trailing=dynamic_trailing,
            breakeven_locked=breakeven_locked,
            peak_profit_pct=round(peak_profit_pct, 4),
        )
        self._append(rec)
        logger.info(
            "[JOURNAL] %s EXIT %s %s @ $%.2f→$%.2f | net=$%.4f (%.2f%%) ROE=%.1f%% | %s",
            strategy, direction, market, entry_price, exit_price,
            pnl_net, pnl_pct, roe_pct, reason,
        )

    # ── Record Partial ──

    def record_partial(
        self,
        strategy: str,
        market: str,
        direction: str,
        entry_price: float,
        exit_price: float,
        qty: float,
        reason: str,
        leverage: int = 1,
    ):
        """부분 청산 기록."""
        if direction.upper() == "LONG":
            pnl_gross = (exit_price - entry_price) * qty
        else:
            pnl_gross = (entry_price - exit_price) * qty

        fee = (entry_price * qty + exit_price * qty) * FEE_RATE
        pnl_net = pnl_gross - fee
        margin = entry_price * qty / leverage if leverage > 0 else entry_price * qty

        rec = TradeRecord(
            ts=time.time(),
            strategy=strategy.upper(),
            event="PARTIAL",
            market=market,
            direction=direction,
            price=exit_price,
            qty=qty,
            entry_price=entry_price,
            exit_reason=reason,
            pnl_gross=round(pnl_gross, 4),
            fee=round(fee, 4),
            pnl_net=round(pnl_net, 4),
            leverage=leverage,
            margin=round(margin, 4),
        )
        self._append(rec)

    # ── Query ──

    def get_trades(
        self,
        limit: int = 50,
        strategy: str = "",
        market: str = "",
        include_blocked: bool = True,
        page: int = 1,
    ) -> Dict:
        """거래 조회 (페이지네이션 + 필터). ★ 캐시 기반 (leak fix 2026-05-15)."""
        self._ensure_cache()
        # 필터링 — 캐시 list 순회 (파일 read 없음)
        if strategy or market or not include_blocked:
            strat_u = strategy.upper() if strategy else ""
            mkt_u = market.upper() if market else ""
            trades: List[Dict] = []
            for rec in self._cache:
                if strat_u and rec.get("strategy") != strat_u:
                    continue
                if mkt_u and rec.get("market") != mkt_u:
                    continue
                if not include_blocked and rec.get("event") == "BLOCKED":
                    continue
                trades.append(rec)
        else:
            trades = self._cache  # 필터 없으면 참조만 (복사 X)

        total_count = len(trades)
        # 페이지 슬라이싱 (오래된 순 저장 → page=1이 최신)
        end_idx = total_count - ((page - 1) * limit)
        start_idx = max(0, end_idx - limit)
        end_idx = max(0, end_idx)
        return {"trades": trades[start_idx:end_idx], "total_count": total_count}

    def get_markets(self) -> List[str]:
        """고유 마켓 목록 조회. ★ 캐시 기반."""
        self._ensure_cache()
        markets = {rec.get("market") for rec in self._cache if rec.get("market")}
        return sorted(markets)

    def get_summary(self) -> Dict[str, Any]:
        """전략별 성과 요약."""
        trades = self.get_trades(limit=9999, include_blocked=True)["trades"]
        exits = [t for t in trades if t.get("event") == "EXIT"]

        # ★ Today reset timestamp (07:00 KST = 22:00 UTC)
        import datetime as _dt
        now_utc = _dt.datetime.now(_dt.timezone.utc)
        _reset_h = 22
        _today_reset = now_utc.replace(hour=_reset_h, minute=0, second=0, microsecond=0)
        if now_utc.hour < _reset_h:
            _today_reset -= _dt.timedelta(days=1)
        _reset_ts = _today_reset.timestamp()

        summary = {}
        for strat in ("FOCUS", "HARPOON"):
            s_exits = [t for t in exits if t.get("strategy") == strat]
            wins = [t for t in s_exits if t.get("pnl_net", 0) > 0]
            losses = [t for t in s_exits if t.get("pnl_net", 0) <= 0]
            total_pnl = sum(t.get("pnl_net", 0) for t in s_exits)
            total_fee = sum(t.get("fee", 0) for t in s_exits)

            # Dynamic trailing 사용 시 성과
            dt_exits = [t for t in s_exits if t.get("dynamic_trailing")]
            dt_pnl = sum(t.get("pnl_net", 0) for t in dt_exits)
            no_dt_exits = [t for t in s_exits if not t.get("dynamic_trailing")]
            no_dt_pnl = sum(t.get("pnl_net", 0) for t in no_dt_exits)

            win_pnl = sum(t.get("pnl_net", 0) for t in wins)
            loss_pnl = sum(t.get("pnl_net", 0) for t in losses)

            summary[strat] = {
                "total_trades": len(s_exits),
                "wins": len(wins),
                "losses": len(losses),
                "win_rate": round(len(wins) / len(s_exits) * 100, 1) if s_exits else 0,
                "total_pnl": round(total_pnl, 4),
                "total_fee": round(total_fee, 4),
                "avg_pnl": round(total_pnl / len(s_exits), 4) if s_exits else 0,
                "best_trade": round(max((t.get("pnl_net", 0) for t in s_exits), default=0), 4),
                "worst_trade": round(min((t.get("pnl_net", 0) for t in s_exits), default=0), 4),
                # 금액 기준 승률: win_pnl / (win_pnl + |loss_pnl|) × 100
                "win_pnl": round(win_pnl, 4),
                "loss_pnl": round(loss_pnl, 4),
                # Dynamic trailing 비교
                "dt_trades": len(dt_exits),
                "dt_pnl": round(dt_pnl, 4),
                "no_dt_trades": len(no_dt_exits),
                "no_dt_pnl": round(no_dt_pnl, 4),
                # ★ Today PnL (07:00 KST reset)
                "today_pnl": round(sum(t.get("pnl_net", 0) for t in s_exits if t.get("ts", 0) >= _reset_ts), 4),
                "today_trades": sum(1 for t in s_exits if t.get("ts", 0) >= _reset_ts),
            }

        # ★ [2026-05-31 부모] PnL 기산점 명확화 — 첫 EXIT ts (journal 파일 시작점).
        #   부모님 통찰: "PnL 기산점이 정확하면 좋은데 사실 그것이 모호" → since 표시로 해결.
        first_exit_ts = min((t.get("ts", 0) for t in exits if t.get("ts", 0) > 0), default=0)

        summary["combined"] = {
            "total_pnl": round(sum(s.get("total_pnl", 0) for s in summary.values() if isinstance(s, dict)), 4),
            "total_trades": sum(s.get("total_trades", 0) for s in summary.values() if isinstance(s, dict)),
            "total_fee": round(sum(s.get("total_fee", 0) for s in summary.values() if isinstance(s, dict)), 4),
            "today_pnl": round(sum(s.get("today_pnl", 0) for s in summary.values() if isinstance(s, dict)), 4),
            "today_trades": sum(s.get("today_trades", 0) for s in summary.values() if isinstance(s, dict)),
            "first_exit_ts": first_exit_ts,  # ★ PnL 기산점 (journal 첫 EXIT)
        }

        return summary


# ── Singleton ──
journal = TradeJournal()
