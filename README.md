# Automated Trading Bot

Personal trading bot for US stocks and ETFs on Interactive Brokers (IBKR). You choose the symbols and strategies. The bot watches prices, places the trades that pass its risk checks, manages the exits and alerts you.

It runs on an IBKR **paper account** (simulated money). Live trading stays off until the checks in [ROADMAP.md](ROADMAP.md) pass.

![Dashboard](docs/dashboard.png)

**Docs:** this README (what it does, how to run it locally) · [DEPLOYMENT.md](DEPLOYMENT.md) (running and operating the full stack) · [ROADMAP.md](ROADMAP.md) (open work).

## Stack

Python 3.11 + FastAPI · PostgreSQL 15 + TimescaleDB · Redis 7 · IBKR (`ib_async`) · React 18 + TypeScript + Tailwind · Docker Compose

## Quick Start

Requires Docker Desktop, Node.js 18+, Python 3.11+, an IBKR paper login and the IBKR Mobile app.

First time:
```bash
cp .env.example .env                 # IBKR paper login for the Gateway container
cp .env.example backend/.env         # backend settings; set SECRET_KEY
(cd backend && pip install -r requirements.txt)
(cd frontend && npm install)
```

Every time, one command starts IB Gateway, Postgres and Redis (Docker), runs migrations, then the API (:8000, docs at /docs) and the UI (:5173):
```bash
scripts/dev.sh          # up; BROKER=mock scripts/dev.sh runs without IBKR
scripts/dev.sh status   # what is running, broker connection and what to do
scripts/dev.sh down     # stop everything
```
Logs: `logs/dev-backend.log`, `logs/dev-frontend.log`.

To start trading on paper: add symbols on the Watchlist page, then enable a strategy and assign symbols on the Strategies page. Every strategy starts disabled with no symbols.

## Configuration

`.env.example` explains every setting. Real values live in gitignored files:

| File | Used by |
|------|---------|
| `.env` | Docker Compose in development (Gateway login) |
| `backend/.env` | A backend run on your machine |
| `.env.prod` | The full stack, see [DEPLOYMENT.md](DEPLOYMENT.md) |

`SECRET_KEY` and `DATABASE_URL` are required: generate the key with `python -c "import secrets; print(secrets.token_hex(32))"`. `BROKER` defaults to `mock`; `scripts/dev.sh` sets `BROKER=ibkr` and the Gateway host and port itself, and variables set on the command line override the file.

## IB Gateway

IBKR has no API keys. The bot talks to IB Gateway, IBKR's desktop app, over a local socket, and the Gateway holds the logged-in session.

The Gateway runs in Docker and logs in by itself with the paper username and password from `.env`, restarting daily. You only approve a login in IBKR Mobile about once a week. Quit the native Gateway app first: same login, same port.

```bash
docker compose --profile ibkr up -d ib-gateway   # development: API on 127.0.0.1:4002
scripts/bot.sh up                                # full stack, see DEPLOYMENT.md
```

| Backend runs | `IBKR_HOST` | `IBKR_PORT` |
|--------------|-------------|-------------|
| On the host (`uvicorn`) | `127.0.0.1` | `4002` |
| In `docker-compose.yml` | `ib-gateway` | `4004` |

Live ports (4001 native, 4003 Docker) are never used, and the bot refuses to trade if the Gateway is logged into a non-paper account. When the Gateway needs you, the dashboard banner and `scripts/dev.sh status` say what to do. Each connection needs its own `IBKR_CLIENT_ID`: the app uses `1`, so give test scripts another number.

## How a trade happens

```
Strategy signal → spread and liquidity checks → RiskManager → OrderManager → IBKR
                                                                  │ filled
PositionMonitor watches the price and closes the trade ◄──────────┘
Reconciler compares the bot's trades with IBKR's positions every 2 minutes
```

1. On NYSE trading days, 09:45-15:45 ET, the scheduler runs each enabled strategy every 60 seconds on its assigned symbols.
2. Each strategy, symbol, day and action can act only once.
3. The risk checks below decide whether to trade and how many shares to buy. Quantities are always whole shares: IBKR's API rejects fractional stock orders.
4. After the fill, a stop order rests at IBKR in case the bot goes offline (`BROKER_PROTECTIVE_STOPS`). The profit target stays inside the bot.
5. The trade closes on its stop, trailing stop, target, time limit (`STALE_TRADE_MAX_DAYS`), day-trade deadline, or your manual close or flatten. These act only while the market is open, so an overnight gap fills at the next open.

Trade status: `PENDING → SUBMITTED → OPEN → CLOSING → CLOSED`, or `CANCELLED` if it never opened. `logs/audit.log` records every order before and after it reaches the broker. It is append-only and never rotated.

## Risk rules

These apply to entries only. Nothing here can block or delay an exit.

- **1% rule:** a trade may lose at most 1% of the account if its stop is hit. Shares = `floor(equity × 1% ÷ (entry − stop))`. This is fixed in code, not a setting.
- **Stop required:** a trade with no valid stop below the entry price is rejected. The bot only buys (long only, no shorting).
- **Reward at least 2× risk** (`MIN_REWARD_TO_RISK`): the target defaults to `entry + 2 × (entry − stop)`.
- **Total open risk** at most 5% of the account (`MAX_PORTFOLIO_RISK_PCT`; can't be set above 10%).
- **No margin:** open positions plus the new trade may not be worth more than the account.
- **One active trade per symbol.**
- **Circuit breaker:** a 3% daily loss (`DAILY_LOSS_LIMIT_PCT`) halts new entries for the day, and 5 losses in a row (`MAX_CONSECUTIVE_LOSSES`) stop that strategy until you re-enable it.

## Growth mode (small accounts)

At very small balances, fees can cost more than a trade could reasonably earn. Growth mode adds restrictions while equity is under $2,000 (`GROWTH_MODE_EXIT_EQUITY`); it never loosens the rules above. IBKR Lite has no API access, so the bot pays IBKR Pro fees: **Fixed** $0.005/share, $1 minimum, or **Tiered** $0.0035/share, $0.35 minimum plus exchange fees, both capped at 1% of trade value (rates as of October 2026). Set yours in `COMMISSION_PLAN`.

Entry checks, in order. Rejections appear in `GET /api/v1/signals`.

| Check | Setting | Rejection |
|-------|---------|-----------|
| Bid/ask spread at most 0.20% | `MAX_SPREAD_BPS` | retried next cycle |
| 20-day average dollar volume at least $20M | `MIN_AVG_DOLLAR_VOLUME` | `INELIGIBLE` |
| No active trade in this symbol | | `SYMBOL_ALREADY_ACTIVE` |
| Risk rules above | | `RISK_LIMIT_EXCEEDED` and similar |
| Growth mode: symbol is one of 14 liquid ETFs | `GROWTH_UNIVERSE` | `GROWTH_SYMBOL_NOT_ALLOWED` |
| Break-even win rate after fees at most 45% | `MAX_BREAKEVEN_WIN_RATE`, `SLIPPAGE_BPS` | `COSTS_TOO_HIGH` |
| Position value within the account | | `NOTIONAL_LIMIT_EXCEEDED` |
| Growth mode: at most 2 active trades | `GROWTH_MAX_OPEN_POSITIONS` | `GROWTH_MAX_POSITIONS` |
| Cash account: order fits in settled cash | `ACCOUNT_TYPE=cash` | `INSUFFICIENT_SETTLED_CASH` |
| Margin account: a day trade remains | `ACCOUNT_TYPE=margin` | `NO_DAY_TRADES_LEFT` |

**What to expect:** holding cash for days or weeks is the intended result at small sizes. Example with a $25 ETF, a stop 2% below entry and a target of twice the risk:

| Equity | Max loss (1%) | Shares | Fees + slippage, Tiered / Fixed | Win rate to break even, Tiered / Fixed | 45% fee check, Tiered / Fixed |
|--------|---------------|--------|---------------------------------|----------------------------------------|-------------------------------|
| $100 | $1.00 | 2 | $0.77 / $1.05 | 59% / 68% | blocked / blocked |
| $250 | $2.50 | 5 | $0.87 / $2.13 | 45% / 62% | passes (barely) / blocked |
| $500 | $5.00 | 10 | $1.03 / $2.25 | 40% / 48% | passes / blocked |
| $1,000 | $10.00 | 20 | $1.34 / $2.50 | 38% / 42% | passes / passes |

The bot starts trading around **$250** on Tiered and **$700** on Fixed. `trades.pnl` is profit after commissions, and the daily summary compares estimated and actual fees. `TRADING_CAPITAL_LIMIT` makes the bot size and check trades as if the account held that much, for testing on paper; the My Trades page still shows the real balance. `GET /api/v1/trading/status` reports growth mode as `growth_mode`.

**Terms**
- **Risk (R):** dollars lost if the stop is hit: shares × (entry − stop).
- **Round trip:** a buy plus the later sell; both pay fees.
- **Break-even win rate:** share of trades that must win to cover losses and fees: (risk + fees) ÷ (reward + risk).
- **Slippage, bps:** filling at a slightly worse price than planned; 1 bp = 0.01%.
- **Spread:** the gap between the best price buyers offer (bid) and sellers ask (ask).
- **Settled cash, T+1:** sale proceeds become spendable one business day after the sale.
- **Day trade:** buying and selling the same stock on the same day.
- **SMA:** average close of the last N days. **ATR:** typical daily price range. **VWAP:** average price weighted by volume.
- **Regime filter:** buy only while the market (SPY) is above its 200-day average.
- **Gap:** the market opens beyond a price level, so the order fills at the open.

## Strategies

| Type | Buys when |
|------|-----------|
| `trend_pullback` | Price bounces after a dip inside an uptrend. Enters the day after the bounce. The first choice for small accounts. |
| `moving_average` | The 50-day average crosses above the 200-day |
| `stock_trend` | Price crosses above its N-day average |
| `mean_reversion` | Price falls well below its rolling average. `max_trend_pct` limits it to range-bound markets. |
| `intra_week_reversion` | Price dips to the weekly VWAP in an uptrend |
| `bull_bear` | Entries allowed only while SPY is above its long-term average |
| `vwap_reclaim` | Day trade: price climbs back above the day's VWAP |
| `opening_range_breakout` | Day trade: first 5-minute close above the opening range |
| `composite` | Combines other strategies into one signal |

Strategies use completed daily bars, refreshed at 16:30 ET; the two day-trade strategies also use 5-minute bars. Configure them on the Strategies page; changes apply on the next cycle without a restart.

**Backtesting** runs one strategy or a portfolio on stored daily bars. Fee schedule and compounding are options, and the Small account preset applies a $100 balance, Fixed fees and compounding. A bar that gaps past a stop or target fills at its open. Each run needs enough history to warm up: a 50/200 average needs 200 bars before it can trade. The day-trade strategies can't be backtested.

## Pages

| Page | Shows |
|------|-------|
| Home | Plain-English status, pause and resume, alerts, open trades; risk, watchlist and system panels under "Risk & system details" |
| My Trades | Positions and P&L |
| Strategies | Enable, configure and assign symbols |
| Watchlist | Add and remove symbols; open one for its price chart, signals and trades |
| Backtesting | Single-strategy and portfolio backtests |
| System Health | Component status, latency, circuit breaker, alerts, performance by date range |

The daily summary goes out at 16:45 ET. Trades, risk alerts and system alerts go by email (`NOTIFICATION_EMAIL_SMTP`) and SMS (`NOTIFICATION_SMS_TWILIO`).

## Internals

One backend process and one uvicorn worker run everything. `TradingEngine` (`backend/app/core/engine.py`) owns the broker, market data feed, scheduler, position monitor, reconciler, risk monitor and notifier. A Redis lock stops a second instance from starting.

| Path | Holds |
|------|-------|
| `backend/app/api/` | Thin routes under `/api/v1`, WebSocket at `/ws/dashboard` |
| `backend/app/core/` | Strategies, risk, execution, backtesting, jobs |
| `backend/app/brokers/` | `BaseBroker` interface, `IBKRClient`, `MockBroker` |
| `backend/app/data/` | Market data feed and Redis cache |
| `backend/app/db/` | SQLAlchemy models and repositories |
| `backend/app/monitoring/` | JSON logs: `trading`, `risk`, `system`, `error` (10 MB, 5 backups) and `audit` |
| `backend/app/notifications/` | Email and SMS delivery |
| `frontend/src/` | Pages, components, React Query hooks, Zustand live store |

**API responses:** `{"data": ..., "meta": {"timestamp", "request_id"}, "error": null | {"code", "message"}}`.

**Live events** are built by `app/core/events.py` as `{"event", "payload", "timestamp"}`:

| Redis channel | Events |
|---------------|--------|
| `watchlist_prices` | `price_update` |
| `trade_events` | `trade_executed`, `trade_closed` |
| `risk_updates` | `risk_alert` |
| `system_alerts` | `system_alert` |

**Background tasks:**

| Task | Runs |
|------|------|
| Gateway connection supervisor | Every 5 s, reconnecting with 5-60 s backoff |
| Position monitor | Every price tick; database resync every 5 s |
| Strategy cycle | Every 60 s in the trading window, unless paused or the broker is down |
| Reconciler | Every 120 s, at startup and after every reconnect |
| Risk monitor | Every 30 s |
| Feed sync | Every 60 s while the broker is connected |
| Bar refresh, daily summary | 16:30 and 16:45 ET on trading days |

## Development

```bash
cd backend && ruff check . && python -m pytest tests/unit -q
cd frontend && npm run typecheck && npm run lint && npm run build
```

Integration tests erase tables, so they refuse to run unless the database name ends in `_test`:
```bash
docker compose up -d db redis
docker compose exec db createdb -U trading trading_bot_test    # once
cd backend
export DATABASE_URL=postgresql+asyncpg://trading:trading@localhost:5432/trading_bot_test
export REDIS_URL=redis://localhost:6379/1
export SECRET_KEY=local-test-secret-key-0123456789abcdef0123456789
export ENVIRONMENT=development
alembic upgrade head && python -m pytest tests/integration -q
```

Coding rules and guardrails are in [.claude/CLAUDE.md](.claude/CLAUDE.md).
