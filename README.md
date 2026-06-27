# 🚀 GainAlgo

An open-source crypto automated-trading framework — a community project for tuning configs together.
Community & best configs: https://blog.naver.com/gainalgo  (gainalgo.ai coming soon)

> NOT a money machine — an experimental framework you tune together. The bot alone is ~break-even; humans do the final harvest.
> Read DISCLAIMER.md first.

## Philosophy — a partner, not a money printer

The bot tills the field 24/7 with discipline and hard tail-risk guards: it watches
100+ coins at 3 a.m. without emotion, never panic-closes, and keeps stops a human
would be tempted to move. But a bot reacts on *completed* signals — it cannot match
a human's read of the 1-5 second window where fast, thin markets actually turn. That
timing gap is structural; no amount of code fully closes it.

So this is built as a **human + bot team**: the bot prepares the ground and keeps you
out of trouble, and *you* harvest the moment (manual entry/exit is always open).
Expect a teammate, not a magic button. Admitting that limit honestly is what lets us
build sound guards around it — rather than pretend it away.

## Supported exchanges

Manage 4 exchanges / 6 markets from one server and one dashboard (per-exchange isolation of capital, records, and settings).

| Exchange | Futures (USDT-M) | Spot |
|---|:---:|:---:|
| Binance | O | O |
| Bybit | O | O |
| Upbit | - | O |
| Bithumb | - | O |

New exchanges start in paper mode — live trading is an explicit per-exchange opt-in.

## Quick start (single server, paper by default)
1. Unzip into C:\Autocoin\
2. Copy .env.example -> .env and edit it (only the exchange keys you use; withdrawal permission OFF; set DASHBOARD_PASSWORD; keep AUTOBOT_LIVE=0)
3. Run .\run.ps1 (auto-installs Python + packages, then starts the server)
4. Open http://localhost:8000 -> observe in paper mode first

### Defaults = safe
Out of the box: every engine OFF, paper mode, single server. Nothing trades for real automatically.

## ⚠️ Paper != Live (slippage)

Don't trust paper (simulated) profit as if it were real. Paper fills instantly at the signal price, but
live trading pays slippage (buy higher / sell lower) plus fees on both sides, which eats small edges —
this is why "the bot alone is roughly break-even." `paper_slippage_bps` (default 5 bps/side, env
`PAPER_SLIPPAGE_BPS`) makes paper model slippage so it approximates live (10–20 bps is realistic for
thin alts). Always validate with small real funds first.

## License
MIT (LICENSE) — free to modify, distribute, and use commercially; just keep the copyright notice (gainalgo.ai).
