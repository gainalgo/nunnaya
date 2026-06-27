# Changelog

All notable changes to this project are documented in this file.
The format is loosely based on [Keep a Changelog](https://keepachangelog.com/);
entries are grouped by date.

## [Unreleased]

### Fixed

- **A single hyper-volatile coin could wipe a day of gains (tail-risk).** Three
  compounding causes let one trade on a hyper-volatile "Innovation-Zone" coin run
  to a catastrophic loss (observed: a short on a coin down ~65% on the day bounced
  +8.7% and closed at -61% ROE / about -$1,800, erasing the day):
  1. **Dynamic leverage was backwards for risk.** The ATR-based tier gave the
     *most* volatile coins the *highest* leverage (ATR ≥ 2.5% → 7x). A volatile
     coin's normal ±8% swing at 7x is already a -56% ROE move. Disabled the tier
     (`leverage_tier_enabled` default `True → False`) so the engine uses the
     static `leverage` (3x); at 3x the same move is ~-26%, well short of a blow-up.
  2. **The blow-off filter let extreme coins through.** It was a soft score
     penalty applied to chases only, with the "extreme" threshold at 80% — so a
     coin down 65% drew only a small penalty and still traded, and a *fade* entry
     (a short into the crash) was exempt entirely. Lowered the extreme threshold
     (`blowoff_extreme_pct` `80 → 45`) and made it apply to both directions
     (`blowoff_chase_only` `True → False`).
  3. **The emergency ROE cap fired too late.** `hard_roe_cap_roe_pct` was -50%,
     so a position could lose half its margin before the backstop cut it.
     Tightened to -25% to catch a slide earlier when the normal stop-loss is
     bypassed (e.g. paper stop-loss lag or a restart).

- **DCA could still fire several averages within a minute on a high-volatility
  pumped coin.** The falling-knife gate defers averaging on a sharp drop, but it
  is ATR-relative and measures the cumulative drop from the last completed bar —
  on a freshly-pumped, high-ATR coin a fast *staircase* of small drops fires four
  or five adds before the cumulative drop reaches the threshold (the gate catches
  a single sharp candle, not a staircase, so it arrives "too late"). Added a
  minimum gap between averaging adds (`dca_min_gap_sec`, default 60s) so a burst
  is capped to one add per window regardless of ATR; the falling-knife gate still
  handles single sharp candles.

- **Spot dashboard top card never showed win rate or trade count.** The
  "Cumulative PnL · Win Rate" summary card displayed the cumulative amount but
  left the win rate and trade count as a placeholder ("— · — trades"), even
  though the Trade Journal below it showed the correct figures. The card's
  win-rate element was never populated in JavaScript — only the amount was set on
  each status refresh, while the win rate and count (available only from the
  journal summary) were never copied up to the card. Fixed by filling the card
  from the journal summary when the journal loads. Affects all four spot
  dashboards.

- **DCA falling-knife gate missed a crash inside the forming candle.** The
  averaging-down gate judged a hard drop only from the last *completed* 5-minute
  candle, so a fast crash contained within the *currently forming* candle —
  compounded by a ~25-second kline cache — was invisible to it. The bot could
  therefore average down several times within a single minute and stop out
  immediately afterward (observed: six adds in about one minute followed by a
  stop-loss). Fixed by passing the live tick price (fresher than the cached
  kline) into the gate and deferring the add when the live drop from the last
  completed close exceeds the threshold; a shallow pullback ("stalled knife")
  still passes. Affects all four spot exchanges.

- **Paper mode ignored the configured per-exchange budget.** The per-slot budget
  in paper mode was derived from a hardcoded total of 1,000,000 in the quote
  currency, ignoring each exchange's configured `budget`. Paper accounts
  therefore traded on the wrong size — for example Upbit/Bithumb on 1,000,000 KRW
  instead of a configured 10,000,000, and the USDT spot accounts on 1,000,000
  USDT instead of a configured 10,000. Fixed so paper uses `config.budget` when
  set (falling back to the default only when it is zero), matching how live mode
  already handles the budget.

- **Config save silently dropped every setting past position 80 in large
  groups.** The dashboard saves settings by chunking them into multiple POST
  requests to keep each URL within length limits. The loop advanced its cursor
  by **250** per iteration but sliced only **80** fields per request, so any
  field beyond index 80 within a group was never included in any request.
  Groups with roughly 200 fields (e.g. the Regime group) lost more than half
  their settings on every save — toggles appeared to "revert" after a reload
  while smaller groups saved fine, which made the symptom intermittent and hard
  to trace. The backend was never at fault; the values simply never reached it.
  Fixed by aligning the loop stride with the slice size (`250 → 80`), so all
  fields are sent across consecutive chunks. Applies to the three spot
  dashboards. The futures dashboard already used a matching stride and slice and
  was not affected.

- **Spot re-entry cooldown was bypassed on live exits, causing fee-bleeding
  churn.** After a position closed, the same coin could be re-bought only minutes
  later (observed: the same price level bought three times within ~37 minutes,
  each ending in a stop-loss). The cooldown's "last exit" timestamp was recorded
  in only one of several close paths; the live stop-loss / take-profit fill path
  closed positions without recording it, so the 45-minute same-coin cooldown
  never saw those exits and allowed immediate re-entry (paper-mode exits recorded
  it, so the bug only surfaced in live trading). Fixed by recording the timestamp
  in the single journal funnel that every full exit passes through, so paper and
  live — and stop-loss, take-profit, and manual closes — are all covered. Partial
  take-profits do not start the cooldown, since the position stays open.

- **Paper mode could read the live account on exchanges configured paper-first.**
  Position sync, post-entry verification, and margin lookups were gated on the
  global live flag only. An exchange running paper-first on an otherwise-live
  server could therefore query the real account — deleting virtual positions as
  "ghosts" and firing authenticated reads — even though no real orders were sent.
  These paths now honour the effective per-exchange paper state, and the dry-run
  client returns virtual values for them instead of delegating to the real client.

- **Order placement is now idempotent.** Market and stop/take-profit orders carry
  a client-generated order id, so a network timeout or rate-limit retry can no
  longer double-submit the same order (which could otherwise double-fill a position).

- **Per-exchange records no longer collide.** Daily P&L snapshots and gate
  statistics are written to per-exchange paths, so running two futures exchanges
  on one server no longer overwrites each other's history.

### Added

- **Futures: auto-skip hyper-volatile "exchange-warning" coins (manual stays open).**
  Innovation-Zone / pump-dump coins (e.g. BEAT, MUSDT, SLX) repeatedly produced
  the day's single big loss on auto entry — the bot buys the top and eats the
  cliff because it cannot time fast/thin markets. Price and listing age were both
  red herrings (BEAT $2.65 / 218 days old). The clean discriminator is volatility:
  Bybit hides its "Innovation Zone" label from the API but exposes its own risk
  tier as `riskParameters.priceLimitRatioY` (those coins = 0.3 vs BTC ~0.02). A
  coin is flagged only when that risk tier AND a high realised 1h ATR% both hold
  (measured split: winners ≤2.3%, blow-ups ≥4.6%). Flagged coins are skipped from
  **AUTO entry only** — manual entry stays open, since the human can still harvest
  the swings. Configurable in *Guards → Exchange Warning* (`block_hivol_auto`,
  `hivol_risk_ratio_min`, `hivol_atr_pct`); the scanner shows a ⚠️ badge on
  flagged coins. The selector price floor now follows `scanner_min_price_usdt`
  (0.2) instead of a hardcoded $1, so the volatility gate — not a blunt price cut
  — decides. This is the futures analog of spot's investment-warning handling.

- **Reset / fresh-start guide in System Actions.** A short inline note next to
  Clean Slate spells out the order to fully reset: ① Close All positions → ② Run
  Amnesty (release pauses/penalties/re-entry blocks) → ③ Clean Slate (wipe
  journals + reset paper balance) → Restart. Clean Slate alone cannot always
  close already-open positions, so Close All comes first. The note marks which
  steps apply in which mode: ① and ② work in any mode, while ③ Clean Slate is
  paper-only (it refuses in live to protect real records) — so in live, ①② alone
  flatten positions and clear all blocks while keeping the real trade history.
  The note carries a live caution: Close All flattens every position at once,
  including underwater ones, so it locks in real losses — to spare a position you
  would rather hold, close coins individually instead of using Close All.

- **Header event countdown now shows *what* the news is.** The economic-event
  countdown previously showed only the time remaining; it now shows the event
  name (e.g. "Core PCE Price Index m/m") inline and in full in the tooltip. The
  name comes from the ForexFactory USD high-impact feed, which was already being
  fetched but had its title discarded — now captured (`get_events_detailed()`)
  and surfaced through the Event Shield status.

- **Clean Slate (paper) — one-click fresh start.** A button in *Settings →
  System Actions* closes every position across all engines, wipes all trade
  journals (each backed up first), and resets the paper balance and PnL baseline
  — for a clean post-fix or paper-to-live baseline. **Paper-mode only** (it
  refuses in live, so it can never touch real positions). Runs in-process, which
  avoids the file-lock and still-held-position problems you hit when clearing
  journals from an external script.

- **Multi-exchange support — Binance USDT-M futures + spot.** The bot can now
  manage Binance (futures + spot) alongside the existing Bybit (futures + spot),
  Upbit (spot), and Bithumb (spot): four exchanges, six markets, all from a single
  server and a single dashboard. Each exchange is fully isolated — capital, trade
  journals, daily snapshots, gate statistics, and settings are kept per-exchange so
  one never bleeds into another's accounting or tuning. A dashboard toggle switches
  the futures view between exchanges. New exchanges start in paper mode; live
  trading is an explicit per-exchange opt-in.

- **DCA stabilization gate (`dca_stabilize_gate_enabled`, default on).** Before
  averaging down on a losing position, the bot now checks short-timeframe
  momentum and defers the add-buy while the last 5-minute candle is still
  dropping hard (≥ `dca_stabilize_strong_atr` × ATR). Entries already had a
  falling-knife guard; DCA did not, so it could keep adding into a freefall and
  enlarge a position right before it stopped out. Pullback DCA on a stabilized
  price still passes — only knife-catching is blocked.

### Changed

- **README now states the project's philosophy up front.** A "Philosophy — a
  partner, not a money printer" section explains the human + bot division of
  labour and the bot's honest limit (it cannot match a human's timing on
  fast/thin markets), so new users expect a teammate rather than a magic button.

- **Codebase translated to English.** All in-repo text — code comments,
  docstrings, log messages, dashboard UI strings, and the generated
  README/DISCLAIMER — is now English so the project reads cleanly for an
  international audience. A small number of Korean string *values* are kept
  intentionally where they are load-bearing (exchange API responses matched as
  substrings, and signal/status values compared by the dashboard JavaScript);
  translating those would break runtime matching. Multi-language UI support is
  planned — the project already ships English/Korean/Thai dictionaries.
