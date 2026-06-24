# 🚀 GainAlgo

오픈소스 암호화폐 자동매매 프레임워크. 함께 설정(config)을 다듬는 커뮤니티 프로젝트입니다.
An open-source crypto automated-trading framework — a community project for tuning configs together.
커뮤니티 · 최적 설정 공유 / Community & best configs: https://blog.naver.com/gainalgo  (gainalgo.ai 준비중 / coming soon)

> ⚠️ "돈 버는 기계"가 아니라 함께 튜닝하는 실험 프레임워크입니다. 봇 단독 = 본전, 최종 수확은 사람 손.
> NOT a money machine — an experimental framework you tune together. The bot alone is ~break-even; humans do the final harvest.
> 반드시 DISCLAIMER.md 를 먼저 읽으세요. / Read DISCLAIMER.md first.

## 지원 거래소 / Supported exchanges

한 서버·한 대시보드에서 4개 거래소 6개 마켓을 통합 관리 (거래소별 자본·기록·설정 격리).
Manage 4 exchanges / 6 markets from one server and one dashboard (per-exchange isolation of capital, records, and settings).

| Exchange | Futures (USDT-M) | Spot |
|---|:---:|:---:|
| Binance | O | O |
| Bybit | O | O |
| Upbit | - | O |
| Bithumb | - | O |

새 거래소는 paper 모드로 시작 — 실거래는 거래소별 명시적 opt-in.
New exchanges start in paper mode — live trading is an explicit per-exchange opt-in.

## 한국어

### 빠른 시작 (단일 서버 · paper 기본)
1. C:\Autocoin\ 에 압축 풀기
2. .env.example -> .env 복사 후 편집 (쓰는 거래소 키만, 출금권한 OFF / DASHBOARD_PASSWORD / AUTOBOT_LIVE=0 유지)
3. .\run.ps1 실행 (Python+패키지 자동 설치 -> 서버 시작)
4. http://localhost:8000 접속 -> paper 로 먼저 관찰

### 기본값 = 안전
받자마자 모든 엔진 OFF · paper · 단일 서버. 아무것도 자동 실거래하지 않습니다.

## English

### Quick start (single server, paper by default)
1. Unzip into C:\Autocoin\
2. Copy .env.example -> .env and edit it (only the exchange keys you use; withdrawal permission OFF; set DASHBOARD_PASSWORD; keep AUTOBOT_LIVE=0)
3. Run .\run.ps1 (auto-installs Python + packages, then starts the server)
4. Open http://localhost:8000 -> observe in paper mode first

### Defaults = safe
Out of the box: every engine OFF, paper mode, single server. Nothing trades for real automatically.

## ⚠️ Paper ≠ Live (슬리피지 / slippage)

페이퍼(모의) 수익을 실거래 수익으로 믿지 마세요. 페이퍼는 신호가에 즉시 체결되지만, 실거래는
매수=비싸게·매도=싸게(슬리피지)에 수수료가 양방향으로 붙어 작은 수익을 갉아먹습니다. 그래서
"봇 단독은 본전"입니다. `paper_slippage_bps`(기본 5bp/편도, env `PAPER_SLIPPAGE_BPS`)로 페이퍼에
슬리피지를 반영해 실거래에 가깝게 만들 수 있습니다(얇은 알트는 10~20bp 현실적). 실전 전 소액 검증 필수.

Don't trust paper (simulated) profit as if it were real. Paper fills instantly at the signal price, but
live trading pays slippage (buy higher / sell lower) plus fees on both sides, which eats small edges —
this is why "the bot alone is roughly break-even." `paper_slippage_bps` (default 5 bps/side, env
`PAPER_SLIPPAGE_BPS`) makes paper model slippage so it approximates live (10–20 bps is realistic for
thin alts). Always validate with small real funds first.

## 라이선스 / License
MIT (LICENSE) — 자유롭게 수정·배포·상업적 사용 가능, 저작권 표시(gainalgo.ai)만 유지.
MIT — free to modify, distribute, and use commercially; just keep the copyright notice (gainalgo.ai).
