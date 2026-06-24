# ============================================================
# SLArbiter — SL/exit opinion arbiter (master §4.2 gate, A)
# ------------------------------------------------------------
# When several parts (cycle_tp·longhold·trailing·triage) give conflicting
# opinions about a position's SL/exit on the same tick, this makes the single
# final decision.
#   parts = propose only (SLProposal) / execution = caller (manager) / judgment = here.
#
# ★ Pure function: arbitrate() decides from inputs alone — no I/O, no global
#   state, no side effects.
#   → 100% unit-testable, zero live impact while unwired. (DESIGN_A_sl_arbiter_20260617.md)
#
# Core rules (Fable5 §4.2 = INV-3 enforced in code):
#   - has_liquidation asymmetry: futures(True)=fast_cut is life / spot(False)=hold-and-wait allowed
#   - while hold-and-wait (FREEZE) is active, reject trailing TIGHTEN (prevents cutting a held coin)
# ============================================================
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List

logger = logging.getLogger(__name__)

# Action constants (shared by proposals/decisions)
EXIT_NOW = "EXIT_NOW"   # sell now (SL hit, etc.)
FREEZE = "FREEZE"       # no selling, hold and wait (longhold)
TIGHTEN = "TIGHTEN"     # move SL up (trailing)
HOLD = "HOLD"           # do nothing (keep)


@dataclass
class SLProposal:
    """Part → arbiter. One opinion from one part."""
    source: str                 # "cycle_tp" | "longhold" | "trailing" | "triage" | "manual"
    action: str                 # EXIT_NOW | FREEZE | TIGHTEN | HOLD
    new_sl: float = 0.0         # proposed SL price when TIGHTEN
    reason: str = ""


@dataclass
class SLDecision:
    """Arbiter → manager. One final decision."""
    action: str                 # EXIT_NOW | HOLD | TIGHTEN
    new_sl: float = 0.0
    reason: str = ""
    source: str = ""            # source of the adopted proposal


class SLArbiter:
    """Arbitrate SL/exit opinions — pure, no side effects."""

    def arbitrate(self, *, has_liquidation: bool, proposals: List[SLProposal],
                  freeze_active: bool, current_sl: float = 0.0) -> SLDecision:
        """Take proposals and make a single decision. (DESIGN_A §A.3 rule order)

        Args:
            has_liquidation: exchange liquidation present (True=futures fast_cut / False=spot hold-and-wait allowed).
            proposals: this tick's proposals from the parts.
            freeze_active: whether spot hold-and-wait (longhold) eligibility is alive (BTC healthy + registered).
                           In a BTC bear market the caller passes False (decided inside the longhold helper).
            current_sl: current SL price — guards TIGHTEN from lowering it.
        """
        props = list(proposals or [])
        has_exit = any(p.action == EXIT_NOW for p in props)
        tighten = [p for p in props if p.action == TIGHTEN]

        # 1) EXIT_NOW priority — exchange branch (INV-3 core)
        if has_exit:
            exit_p = next(p for p in props if p.action == EXIT_NOW)
            if has_liquidation:
                # Futures: SL hit = sell unconditionally. Ignore FREEZE (liquidation defense = life).
                return SLDecision(action=EXIT_NOW, reason=exit_p.reason or "exit:futures_sl",
                                  source=exit_p.source)
            # Spot: if a valid FREEZE exists, fall to rule 2 (hold-and-wait). Otherwise sell.
            if not freeze_active:
                # BTC bear etc. → normal SL sell (avoid holding a falling knife)
                return SLDecision(action=EXIT_NOW, reason=exit_p.reason or "exit:spot_no_freeze",
                                  source=exit_p.source)
            # freeze_active=True → falls through to rule 2 (HOLD)

        # 2) FREEZE gate (spot only) — while hold-and-wait is alive, do not sell
        if freeze_active and not has_liquidation:
            # 3) Hold-and-wait protection: reject TIGHTEN during this (so trailing can't cut a held coin)
            if tighten:
                logger.debug("[SLArbiter] rejected %d TIGHTEN(s) during hold-and-wait", len(tighten))
            return SLDecision(action=HOLD, reason="freeze:longhold_active", source="longhold")

        # 4) TIGHTEN consensus — SL only moves up (highest new_sl, only if above current_sl)
        if tighten:
            best = max(tighten, key=lambda p: p.new_sl)
            if best.new_sl > current_sl:
                return SLDecision(action=TIGHTEN, new_sl=best.new_sl,
                                  reason=best.reason or "trailing:tighten", source=best.source)
            # proposed SL is at or below current → ignore (prevent backsliding)

        # 5) default
        return SLDecision(action=HOLD, reason="no_actionable_proposal", source="")


# Single module instance (stateless because pure — safe to share)
_arbiter = SLArbiter()


def arbitrate(*, has_liquidation: bool, proposals: List[SLProposal],
              freeze_active: bool, current_sl: float = 0.0) -> SLDecision:
    """Module-level helper — arbitrate via the shared instance."""
    return _arbiter.arbitrate(
        has_liquidation=has_liquidation, proposals=proposals,
        freeze_active=freeze_active, current_sl=current_sl,
    )
