# Automated Trading Bot

Personal trading bot for US stocks and ETFs on Interactive Brokers (IBKR). You choose the symbols and strategies. The bot watches prices, places the trades that pass its risk checks, manages the exits and alerts you.

It runs on an IBKR **paper account** (simulated money). The current MVP uses rule-based strategies. Leadership's hybrid ML/LLM architecture is a proposed extension; the [architecture review](docs/architecture-review.md) records the gaps and implementation order. Live deployment requires the checks in [ROADMAP.md](ROADMAP.md), a separate deployment review, and explicit approval.

![Home: bot status, today's result and open positions](docs/home.png)

![My Trades: profit and loss with trade history](docs/my-trades.png)

*Screenshots use sample data.*

**Docs:** this README (what it does, how to run it locally) · [DEPLOYMENT.md](DEPLOYMENT.md) (running and operating the full stack) · [PRODUCT.md](PRODUCT.md) (audience and design principles) · [ROADMAP.md](ROADMAP.md) (open work) · [Architecture review](docs/architecture-review.md) (leadership alignment, evidence, and priorities).

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

The Gateway runs in Docker and logs in by itself with the paper username and password from `.env`, restarting daily at `IB_AUTO_RESTART_TIME`. IBC inside the image enters the credentials. You only approve a login in IBKR Mobile about once a week; see the [approval and recovery procedure](DEPLOYMENT.md#2-the-weekly-ibkr-mobile-approval). Quit the native Gateway app first: IBKR allows one session per login, and both apps use the same port.

```bash
docker compose --profile ibkr up -d ib-gateway   # development: API on 127.0.0.1:4002
scripts/bot.sh up                                # full stack, see DEPLOYMENT.md
```

| Backend runs | `IBKR_HOST` | `IBKR_PORT` |
|--------------|-------------|-------------|
| On the host (`uvicorn`) | `127.0.0.1` | `4002` |
| In `docker-compose.yml` | `ib-gateway` | `4004` |

Never set `IBKR_TRADING_MODE=live`. Live ports (4001 native, 4003 Docker) are never used, and the bot refuses to trade if the Gateway is logged into a non-paper account. When the Gateway needs you, the dashboard banner and `scripts/dev.sh status` say what to do. Each connection needs its own `IBKR_CLIENT_ID`: the app uses `1`, so give test scripts another number.

## How a trade happens

```
Strategy signal → spread and liquidity checks → RiskManager → OrderManager → IBKR
                                                                  │ filled
PositionMonitor watches the price and closes the trade ◄──────────┘
Reconciler compares the bot's trades with IBKR's positions every 2 minutes
```

1. On NYSE trading days, 09:45-15:45 ET, the scheduler runs each enabled strategy every 60 seconds on its assigned symbols. Creating or changing a strategy while the bot runs downloads 2 years of daily price history for its symbols and any extra symbols it needs (for example SPY, which tracks the whole US market, for the market filter), and the scheduler uses the new bars the same day. The same applies to a history download started from the Watchlist page.
2. Each strategy, symbol, day and action can act only once.
3. The risk checks below decide whether to trade and how many shares to buy. Quantities are always whole shares: IBKR's API rejects fractional stock orders.
4. After the fill, the bot attempts to place a stop order at IBKR in case it goes offline (`BROKER_PROTECTIVE_STOPS`). Confirm that the broker accepted it; placement failure currently alerts without blocking further entries. The profit target stays inside the bot. When the Gateway is logged out, the bot cannot trade and its internal stop monitor receives no prices. With `BROKER_PROTECTIVE_STOPS=true` (the default), only an accepted, working stop provides broker-side protection during an outage; verify its quantity and state directly at IBKR rather than relying on a setting or saved order ID.
5. The trade closes on its stop, trailing stop, target, time limit (`STALE_TRADE_MAX_DAYS`), day-trade deadline, or your manual close or flatten. These act only while the market is open, so an overnight gap fills at the next open. A strategy can also close its own trade with a sell signal, called a signal exit (`SIGNAL_EXIT`): when the strategy that opened the trade produces a SELL, for example `moving_average`'s "death cross" when the 50-day average falls below the 200-day. Signal exits do not run while trading is paused. A SELL never opens a short position (a bet that the price will fall), and it is ignored when there is no open trade, or when the trade was opened by another strategy or manually. If a signal exit fails, or the market turns out to be closed, the strategy tries again on its next run the same day while the trade is still open, and the first failed attempt that day sends a `CLOSE_FAILED` alert.

**Trailing stop:** a stop that moves up as the price rises, locking in more of the gain. The raised stop is saved with the trade whenever it rises by at least 0.1R, so it survives a backend restart. If saving fails, for example because the database is down, the bot waits 5 seconds before trying again; the stop itself keeps working the whole time. R is the per-share risk: entry price minus the original stop. The stop order already resting at IBKR is not moved.

## Trading controls

All endpoints below are under `/api/v1`. `GET /trading/status` reports pause state, engine state and broker connection with a "what to do" hint.

- **Pause/resume:** `POST /trading/pause` and `POST /trading/resume`. Pause state survives restarts. Pause stops automated entries and strategy signal exits; manual buys remain possible, while stops, targets and manual closes keep working during market hours. An account-wide entry halt remains planned work.
- **Close one trade:** `POST /trades/{id}/close` or the UI Close button. Responses: 200 closed, 202 exit pending, 409 not open or market closed, 502 broker failure.
- **Circuit breaker:** `GET /risk/circuit-breaker`; reset only after understanding why it tripped.

**Flatten** (the "Sell everything" button) uses `POST /trading/flatten` with `{"confirm": "FLATTEN"}`; the UI sends that body silently. Flatten is best effort, not a guarantee. It pauses trading, waits up to 30 seconds for strategy buy orders that are being sent, and cancels buy orders still working at IBKR. A buy that filled only partly has its unfilled rest cancelled. Flatten then waits until IBKR reports that cancel as finished, counts the shares that really bought, and sells them. Finally it closes all open trades, including any whose buy remainder was still working. One limit remains: an entry still being sent to IBKR after the 30-second wait can still open. Inspect every returned outcome and verify broker holdings and terminal order states; HTTP success does not prove the account is flat. Resume manually only after account consistency is established.

### Fills and recovery

**Fills:** a **fill** is shares that IBKR has bought or sold for one of the bot's orders. A **partial fill** means only some of the ordered shares have traded so far. Every fill and every commission (the fee IBKR charges per order) is saved to the fill journal, the `trade_fills` table with one row per IBKR execution, tagged `ENTRY` (the buy), `STOP` (the protective stop) or `EXIT` (the sell).

**Trade status:** `PENDING → SUBMITTED → OPEN → CLOSING → CLOSED`, or `CANCELLED` if it never opened. A trade becomes CLOSED only after all its shares have exited, and never while its buy order is still live at IBKR. Its P&L (profit and loss after fees) includes every fill and every fee; an entry commission that IBKR reports late is picked up. If a buy fills partly, the bot cancels the rest, waits up to 5 seconds for the cancel to finish, opens the trade with the shares that filled, and places a stop for exactly those shares. If IBKR has not confirmed the cancel, the `ENTRY_REMAINDER_WORKING` alert is sent. A protective stop that sells only some shares leaves the rest still managed, and the bot sells them later. Entry submission attempts to write before/after records to `logs/audit.log`. The file uses ordinary logging handlers, is opened in append mode and never rotated; never edit it. A failed write can return without failing submission, so durable audit-write enforcement remains open work. The review's other [confirmed execution and recovery gaps](docs/architecture-review.md#findings) are still open too, so check them before relying on trade status or reconciliation as proof that all shares are accounted for.

**Reconciler:** a background check that compares the bot's records with IBKR's positions, orders and executions. It runs every 2 minutes, at startup and after each reconnect. Each run reads IBKR's executions once and journals them. It adopts shares from a partly filled entry and from later fills: `LATE_ENTRY_FILL` is alerted when the entry fills more after the trade opened, and the stop is resized (`PROTECTIVE_STOP_REPLACED`). It compares IBKR's share count with the bot's and sends a `QUANTITY_MISMATCH` alert with kind `DIRECTION` (IBKR holds shares the wrong way, a short position the bot never opens), `MISSING` (fewer shares than the bot expects) or `EXCESS` (more). Each trade and kind alerts at most once per day. The reconciler never places an order to correct a mismatch. It replaces a protective stop that no longer covers the open shares only when IBKR holds at least that many shares in the trade's direction. If IBKR shows no shares but some sales are missing from the journal, it closes the trade, values the missing shares at the last known price and sends `EXIT_FILLS_MISSING`. If the buy order is still live, it cancels the buy and waits for a later run instead of closing. Before closing, it also cancels any stop or sell order still live at IBKR and waits for IBKR to confirm; if one stays live, it sends `CLOSE_FAILED` and keeps the trade open, because that order could otherwise sell shares the account no longer holds. The bot finds its stop and sell orders at IBKR by their order reference, not only by the order ID it saved, so a failed database write cannot leave an order it no longer knows about. Commission reports that arrive after a trade has closed update its journal row, but the trade's P&L is not recalculated.

## Risk rules

These apply to entries only. Nothing here can block or delay an exit.

- **1% sizing rule:** planned price-to-stop risk is capped at 1% of equity. Shares are the smaller of `floor(equity × 1% ÷ (entry − stop))` and `floor(equity ÷ entry)`. The 1% ceiling is fixed in code. Fees, slippage and gaps can make the actual loss larger; all-in loss budgeting remains open work.
- **Stop required:** a trade with no valid stop below the entry price is rejected. The bot only buys (long only, no shorting).
- **Reward at least 2× risk** (`MIN_REWARD_TO_RISK`): the target defaults to `entry + 2 × (entry − stop)`.
- **Aggregate approved risk:** active trade reservations plus the proposed trade's planned price-to-stop risk are capped at 5% of equity (`MAX_PORTFOLIO_RISK_PCT`; can't be set above 10%). Current reservations do not fully reflect late fills or adverse entry prices.
- **No margin:** open positions plus the new trade may not be worth more than the account.
- **One active trade per symbol.**
- **Circuit breaker:** a 3% daily realized loss (`DAILY_LOSS_LIMIT_PCT`) halts new entries for the day, and 5 losses in a row (`MAX_CONSECUTIVE_LOSSES`) stop that strategy until you re-enable it. Unrealized-loss and high-water-mark drawdown controls remain open work.

Entry enforcement is currently split between the scheduler, `RiskManager` and `OrderManager`. Missing bid/ask data can pass the scheduler's spread check, and manual entries do not use its spread/liquidity checks. Failed circuit-breaker recovery, failed or skipped reconciliation, and failed protective-stop placement do not yet establish a shared entry halt. Startup continues into trading loops after a recovery or reconciliation failure. If risk-state recovery, reconciliation or protective-stop placement fails, pause automated entries, avoid manual buys, inspect broker orders and holdings, and investigate before resuming. Treat quantity mismatches as unresolved until checked against broker executions, positions and working orders; OPEN/CLOSED status and reconciliation summaries alone are insufficient. Health and connection status do not establish trading readiness. The shared fail-safe gateway in the architecture review is proposed behavior; its repair target blocks additional exposure while preserving risk-reducing actions.

## Growth mode (small accounts)

The product includes paper experiments with less than $100; this is an operating scenario, not a minimum balance or the growth-mode threshold. At very small balances, fees can cost more than a trade could reasonably earn. Growth mode adds restrictions while equity is under $2,000 (`GROWTH_MODE_EXIT_EQUITY`); it never loosens the rules above. IBKR Lite has no API access, so the bot pays IBKR Pro fees: **Fixed** $0.005/share, $1 minimum, or **Tiered** $0.0035/share, $0.35 minimum plus exchange fees, both capped at 1% of trade value (rates as of October 2026). Set yours in `COMMISSION_PLAN`.

Automated entry checks, in order. Outcomes appear in `GET /api/v1/signals`; broker rejection can currently be mislabeled `SUBMITTED`, and manual creation can return HTTP 201 for a cancelled trade (see the roadmap).

| Check | Setting | Rejection |
|-------|---------|-----------|
| Available bid/ask spread at most 0.20%; missing spread currently passes | `MAX_SPREAD_BPS` | retried next cycle |
| 20-day average dollar volume at least $20M | `MIN_AVG_DOLLAR_VOLUME` | `INELIGIBLE` |
| No active trade in this symbol | | `SYMBOL_ALREADY_ACTIVE` |
| Risk rules above | | `RISK_LIMIT_EXCEEDED` and similar |
| Growth mode: symbol is one of 14 liquid ETFs | `GROWTH_UNIVERSE` | `GROWTH_SYMBOL_NOT_ALLOWED` |
| Break-even win rate after fees at most 45% | `MAX_BREAKEVEN_WIN_RATE`, `SLIPPAGE_BPS` | `COSTS_TOO_HIGH` |
| Position value within the account | | `NOTIONAL_LIMIT_EXCEEDED` |
| Growth mode: at most 2 active trades | `GROWTH_MAX_OPEN_POSITIONS` | `GROWTH_MAX_POSITIONS` |
| Cash account: order fits in reported settled cash, falling back to cash balance if missing | `ACCOUNT_TYPE=cash` | `INSUFFICIENT_SETTLED_CASH` |
| Margin account: reported day trades remaining is not zero | `ACCOUNT_TYPE=margin` | `NO_DAY_TRADES_LEFT` |

The margin check represents the current implementation. Confirm the broker/account's applicable margin regime before extending support; FINRA's replacement intraday-margin standards took effect June 4, 2026, with broker phase-in through October 20, 2027. See [FINRA Regulatory Notice 26-10](https://www.finra.org/rules-guidance/notices/26-10), checked November 13, 2026.

**What to expect:** holding cash for days or weeks is the intended result at small sizes. Example with a $25 ETF, a stop 2% below entry and a target of twice the risk:

| Equity | Planned price-to-stop risk (1%) | Shares | Fees + slippage, Tiered / Fixed | Win rate to break even, Tiered / Fixed | 45% fee check, Tiered / Fixed |
|--------|---------------|--------|---------------------------------|----------------------------------------|-------------------------------|
| $100 | $1.00 | 2 | $0.77 / $1.05 | 59% / 68% | blocked / blocked |
| $250 | $2.50 | 5 | $0.87 / $2.13 | 45% / 62% | passes (barely) / blocked |
| $500 | $5.00 | 10 | $1.03 / $2.25 | 40% / 48% | passes / blocked |
| $1,000 | $10.00 | 20 | $1.34 / $2.50 | 38% / 42% | passes / passes |

These examples are estimates from the configured cost model, using 5 bps slippage per leg. They do not establish a minimum starting balance or a profitable strategy. Actual eligibility depends on price, stop distance, available cash and fees. At $1,000 in the Tiered example, $10 of price-to-stop risk plus $1.34 of modeled friction gives an estimated $11.34 loss at the stop, before gaps.

`trades.pnl` is recorded profit after commissions, built from every fill in the `trade_fills` journal. A commission that IBKR reports after a trade has closed updates the journal but not `pnl`. The daily summary compares estimated and actual fees. `TRADING_CAPITAL_LIMIT` caps sizing equity; it does not create a separate small-account cash/equity ledger. A large paper account capped at $100 can still report $100 of sizing equity after a $10 loss. Record broker and experimental equity separately; small-account certification requires the virtual cash/equity ledger planned in the roadmap. The My Trades page shows the real broker balance. `GET /api/v1/trading/status` reports growth mode as `growth_mode`.

The break-even threshold is a cost filter; it does not predict the strategy's win rate. Include data, hosting and AI expenses in validation: a hypothetical $10/month costs $120/year, or 12% of $1,000 of capital before trading costs.

**Terms**
- **Risk (R):** planned price-to-stop risk: shares × (entry − stop), before fees, slippage and gaps.
- **Round trip:** a buy plus the later sell; both pay fees.
- **Break-even win rate:** share of trades that must win to cover losses and fees: (risk + fees) ÷ (reward + risk).
- **Slippage, bps:** filling at a slightly worse price than planned; 1 bp = 0.01%.
- **Spread:** the gap between the best price buyers offer (bid) and sellers ask (ask).
- **Settled cash, T+1:** sale proceeds become spendable one business day after the sale.
- **Day trade:** buying and selling the same stock on the same day.
- **SMA:** average close of the last N days. **ATR:** typical daily price range. **VWAP:** average price weighted by volume.
- **Regime filter:** buy only while the market (SPY) is above its 200-day average.
- **Gap:** the market opens beyond a price level, so the order fills at the open.

## Dividend data

A dividend is cash a company pays its shareholders. The **ex-dividend date** is the cutoff: you must own the stock before this date to get the payment. A **dividend cut** is when a company lowers or stops its payment.

Once a day the backend fetches dividend history (paid and declared future payments) for each symbol in `DIVIDEND_SYMBOLS` from Alpha Vantage's free `DIVIDENDS` API and stores it in the `dividend_events` table (migration `0013`). It places no orders and changes no trading behavior. `app/core/dividends.py` computes trailing and forward yield (special one-off dividends excluded) and payment frequency (from the 3 latest gaps between ex-dates). `app/core/dividend_cuts.py` detects cuts (the latest payment, annualized, below the previous one) and missed payments, for later dividend features. A drop to exactly 1/2, 1/3 ... 1/10 is treated as a stock split, so a real cut to exactly that size is missed.

| Setting | Default | Meaning |
|---------|---------|---------|
| `ALPHA_VANTAGE_API_KEY` | empty | Free key from alphavantage.co. While empty, the job does nothing and logs that once a day. |
| `DIVIDEND_REFRESH_DAILY_BUDGET` | `20` | Most Alpha Vantage requests the job may make per day, 1 to 25. The free tier allows 25, so 5 stay free for manual use. |
| `DIVIDEND_SYMBOLS` | `[]` | JSON list of symbols to track, e.g. `["KO","PEP"]`. |

The budget is counted in Redis per US Eastern date. Every HTTP attempt counts, including retries and failures. Symbols are refreshed oldest-first; any left over when the budget runs out are logged and go first the next day. A symbol fetched in the last 7 days is skipped unless one of its stored ex-dividend dates has passed since. A failed or rate-limited reply never overwrites stored data. Rows that can't be stored are dropped while parsing: a repeated ex-dividend date (the first, newest row is kept) or an amount of 10 billion or more. If the database still rejects one symbol's data, that symbol is logged as "Dividend store failed", left unmarked so it's retried next day, and the run carries on with the rest. Any other database error stops the run.

These settings are not in `.env.example` yet. When you add them to `.env`, add them to `.env.example` too, with a comment.

## Strategies

| Type | Buys when |
|------|-----------|
| `trend_pullback` | Price bounces after a dip inside an uptrend. Enters the day after the bounce. The first candidate for small-account validation. |
| `moving_average` | The 50-day average crosses above the 200-day |
| `stock_trend` | Price crosses above its N-day average |
| `mean_reversion` | Price falls well below its rolling average. `max_trend_pct` limits it to range-bound markets. |
| `intra_week_reversion` | Price dips to the weekly VWAP in an uptrend |
| `bull_bear` | Entries allowed only while SPY is above its long-term average |
| `vwap_reclaim` | Day trade: price climbs back above the day's VWAP |
| `opening_range_breakout` | Day trade: first 5-minute close above the opening range |
| `composite` | Combines other strategies into one signal |

Strategies use completed daily bars, refreshed at 16:30 ET; the two day-trade strategies also use 5-minute bars. Configure them on the Strategies page; changes apply on the next cycle without a restart.

**Backtesting** runs one strategy or a portfolio on stored daily bars. Fee schedule and compounding are options, and the Small account preset applies a $100 balance, Fixed fees and compounding. A bar that gaps past a stop or target fills at its open. Each run needs enough history to warm up: a 50/200 average needs 200 bars before it can trade. The day-trade strategies can't be backtested. Backtests do not simulate signal exits.

Backtests do not yet share all execution constraints: growth-universe and position limits, aggregate capital reservations and settlement behavior are incomplete. The review reproduced about $2,001 of simultaneous simulated positions on $1,000 while execution rejects the second order. Reported drawdown uses closed-trade P&L, and Sharpe treats per-trade observations as daily returns. A chronological train/test helper exists, but rolling validation, extra-symbol history support in that helper and marked-to-market metrics remain open work. Current results support investigation; they are insufficient for live approval.

## Pages

The dashboard uses a dark theme; retain it when changing the UI. Watchlist, backtesting, system health and strategy parameters are optional tools, kept out of the main flow.

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

One backend process and one uvicorn worker run everything. `TradingEngine` (`backend/app/core/engine.py`) owns the broker, market data feed, scheduler, position monitor, reconciler, risk monitor and notifier. A Redis lease selects the leader for trading loops. Manual mutation routes do not yet enforce leadership, so run only one backend instance; the lease does not make multiple replicas safe.

| Path | Holds |
|------|-------|
| `backend/app/api/` | Thin routes under `/api/v1`, one dashboard WebSocket at `/ws/dashboard` |
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

These Redis Pub/Sub events are transient; they do not provide durable decision replay. Input bars can change on refresh. Backups preserve available records, but do not establish complete decision lineage or deterministic replay. Leadership's proposed pipeline adds versioned inputs, proposals, deterministic authorizations, an execution journal and recoverable dispatch. ML/LLM workers would propose signals and research without broker mutation access.

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
