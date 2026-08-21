# FX Live Board

Live prices for 12 pairs (XAUUSD, GBPCAD, USDCAD, EURCAD, GBPUSD, USDJPY,
EURJPY, USDCHF, EURGBP, EURUSD, CADJPY, GBPJPY) from your MetaTrader 5
account (investor/read-only login), plus a per-symbol technical analysis
view that overlays a multi-layer fundamental+technical+scalping framework
directly on a live candlestick chart.

## Architecture

MetaTrader 5 is Windows-only and its Python API needs to talk to a real,
natively-running terminal — running it under Wine inside a Linux container
turned out to be unreliable (persistent IPC timeouts talking to the
terminal). So the bridge runs natively on this Windows machine instead, and
only the web dashboard is containerized:

- **windows-bridge/** — a FastAPI app that runs directly on Windows (not in
  Docker) using the official `MetaTrader5` pip package.
  - `app.py` — logs into your account, polls all 12 symbols every second,
    serves them over WebSocket (`/ws`) and REST (`/api/prices`), and exposes
    `/api/analysis/{symbol}` for the per-symbol analysis engine.
  - `analysis.py` — the strategy engine (see below).
- **frontend** (Docker) — nginx serving the dashboard, reverse-proxying
  `/api` and `/ws` to the native bridge via `host.docker.internal:8000`.
  - Collapsible left sidebar (hamburger toggle) lists the Board and all 12
    symbols; each symbol's H4 bias is shown as a colored tag.
  - **Board view**: the live price table (all 12 pairs).
  - **Symbol detail view**: a candlestick chart (M5, via
    [lightweight-charts](https://github.com/tradingview/lightweight-charts))
    with the strategy's key levels drawn directly on it — sweep, MSS, order
    block, FVG, OTE zone, entry/SL/TP — plus bias badges, a kill-zone
    indicator, and a plain-language summary/forecast panel. Deep-linkable
    via `#symbol=XAUUSD.`.

MT5 terminal used: `C:\Program Files\Mond Trades MT5 Terminal\terminal64.exe`
(already installed on this machine). This broker suffixes every symbol with
a trailing dot (`XAUUSD.`, `GBPUSD.`, ...) — set via `MT5_SYMBOLS` in `.env`.

## The strategy framework (`windows-bridge/analysis.py`)

Implements the 5-layer framework as follows — **layers 1 and the COT part
of positioning are NOT wired to a live data feed** (no news/calendar/COT API
is connected); everything else runs off real MT5 price data:

| Layer | What's implemented | Data source |
|---|---|---|
| 1. Weekly fundamental bias | **Manual only** — a per-symbol bias + notes field in the UI (saved to browser localStorage), clearly labeled as manual input | none (by design — see below) |
| 2. H4/Daily structure | Swing high/low detection (fractal method) → HH/HL = bullish, LH/LL = bearish, else neutral | MT5 H4 + D1 candles |
| 3. Daily bias | Previous day high/low + whether swept, Asia session range, daily open | MT5 D1 + H1 candles |
| 4. Kill-zone filter | Asia/London/New York/London-Close windows in real New York time (DST-aware via `zoneinfo`) | system clock |
| 5. Entry mechanism | Liquidity sweep → Market Structure Shift (displacement candle) → Order Block + Fair Value Gap (Unicorn if they overlap) → OTE zone (61.8%–79% fib of the displacement leg) → entry/SL (beyond swept extreme + buffer)/TP (opposite liquidity pool or 1:2 RR) | MT5 M5 candles |

Layer 1 and COT positioning were left manual/unimplemented rather than faked,
since there's no economic-calendar, central-bank-statement, or COT-report
feed wired in — building that out (scraping ForexFactory/Investing.com,
parsing COT reports, etc.) is a separate project. Say the word if you want
that added.

## Account dashboard (`windows-bridge/account.py`)

A second sidebar tab ("حساب و پوزیشن‌ها") shows, refreshed every 3s:
balance/equity/margin, floating P&L, open positions with per-position risk,
recent closed trades + total, total open risk (money + % of balance), and
daily/weekly/monthly P&L % (against an estimated start-of-period balance,
not current balance — otherwise a big realized loss makes the % look
absurd once the balance has already shrunk).

## Automated trading (`windows-bridge/trading.py`)

Entry logic: M15 liquidity sweep, confirmed by a same-direction M5 sweep,
only taken if it agrees with the H4 trend (layer 2) — ambiguous H4
structure means no trade. SL sits beyond the swept extreme; 3 TP targets
are computed (nearest liquidity pools where the structure gives them,
filled out with 1R/2R/3R otherwise) — a single MT5 position can only carry
one `tp`, so the real order uses TP1. Positions this engine opens (tagged
with a magic number) are trailed automatically once 1R in profit.

**Disabled by default.** Toggle it from the Account tab. It also depends on
the MT5 terminal's own "AutoTrading" button being on (a local setting the
API can't flip) and on your broker allowing algorithmic/API trading on the
account at all — check with your broker if orders get rejected with
`AutoTrading disabled by server`.

Position size is a fixed lot (`TRADE_LOT_SIZE` in `.env`, default `0.01`),
also adjustable live from the Account tab.

## Telegram signals (`windows-bridge/signals.py`)

Every time the strategy above finds a fresh confirmed signal (M15+M5 sweep
+ H4 trend), it's posted to your Telegram channel as a chart image (entry/
SL/TP1/TP2/TP3 drawn on the candles) with the same levels spelled out in
the caption — **this happens regardless of whether real order placement is
enabled or even possible**, since it only needs price data, not trading
permission. The engine then tracks live price against that signal and
replies on the same message when SL or a TP is hit, until TP3 or SL closes
it out. Tracking is in-memory only — a bridge restart clears it (open
signals aren't picked back up).

Configured via `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in `.env`; unset
either one to disable Telegram entirely (the strategy engine and MT5
trading keep working independently of it).

**Session-close summaries:** right after each major FX session closes
locally (Sydney/Tokyo/London/New York — DST-aware via `zoneinfo`), a post
lists every signal opened during that session with its outcome (TP/SL/
still tracking) and the win/loss % across the ones that resolved. Checked
every trading-loop cycle (~20s), so it fires within a couple minutes of
the actual close; fires once per session per day.

## Run it

**1. Start the native bridge** (run this every time you want the board live;
keep the window open):

```
cd windows-bridge
.\run.ps1
```

It reads credentials from `..\.env` and serves on `http://localhost:8000`.

**2. Start the dashboard container:**

```
docker compose up -d --build
```

Open the board:

```
http://localhost:8080
```

## Credentials

Stored in `.env` in this project root (git-ignored, never commit it).

**Important:** the investor password was pasted directly into chat.
Rotate it from your broker's client portal once this system is confirmed
working, and update `.env` with the new one (then restart `run.ps1`). The
investor password is read-only (no trading rights), which limits the blast
radius, but treat it as compromised regardless.

## Keeping the bridge running long-term

Right now `run.ps1` needs to be started manually and stays tied to its
terminal window. If you want it to survive reboots / run in the background
permanently, options include:
- A Scheduled Task (Task Scheduler) that runs `run.ps1` at logon.
- Wrapping it as a Windows service (e.g. with NSSM).

Ask if you'd like either of these set up.

## Stopping

- Bridge: close its PowerShell window (or Ctrl+C).
- Dashboard: `docker compose down`
