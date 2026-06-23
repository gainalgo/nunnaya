# Changelog

All notable changes to this project are documented in this file.
The format is loosely based on [Keep a Changelog](https://keepachangelog.com/);
entries are grouped by date.

## [Unreleased]

### Fixed

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
