# tasks.md — Implementation Task Tracker

> Status: `[ ]` todo · `[~]` in progress · `[x]` done
> Effort: S = ~2–4hrs · M = ~1 day · L = ~2–3 days · XL = 3+ days

> See `design.md` for architecture. See `requirements.md` for acceptance criteria. See `ibkr-gateway.md` for IB Gateway setup and authentication. See `environment-setup.md` for environment variable setup.

---

## ⚠️ Library Change: `ib_insync` → `ib_async`

The original `ib_insync` library is **no longer maintained**. Its author, Ewald de Wit, passed away in early 2024. IBKR's own documentation now directs users to migrate to **`ib_async`**, a community fork maintained at [github.com/ib-api-reloaded/ib_async](https://github.com/ib-api-reloaded/ib_async).

**Impact on this project:**
- Replace `ib_insync` with `ib_async` in `requirements.txt`
- Import path changes from `from ib_insync import IB` → `from ib_async import IB`
- API surface is intentionally compatible — method names and behavior are preserved
- TWS/Gateway version requirement: 1023 or higher (unchanged)
- Default ports: 7497 (TWS), 4001 (Gateway)

All task references to `ib_insync` below mean `ib_async`.

---

## Approach: One Layer at a Time

Tasks are ordered so that each layer is **fully built and testable** before the next begins. You will always have a working, runnable system at the end of each group — never a pile of half-built components.

The build order is:
1. Project skeleton (repo, Docker, CI)
2. Config and logging (foundation every other layer depends on)
3. Database layer (models, migrations, repositories)
4. Broker abstraction (mock-first, then real IBKR)
5. Market data (live feed + historical)
6. Risk engine (calculator → manager → monitor)
7. Trade execution (order manager + audit trail)
8. API layer (REST endpoints + WebSocket)
9. Frontend shell (routing, layout)
10. Frontend panels (wired to live data)
11. Strategy engine (base class → first strategy → scheduler)
12. Advanced features (backtesting, metrics, notifications)

---

## Phase 1 — Foundation

### Layer 1 — Project Skeleton

These tasks create a runnable shell with nothing inside it yet. After this layer you can run `docker-compose up` and see an empty FastAPI app respond on localhost.

- [x] **1.1** Initialize monorepo: create `backend/` and `frontend/` directories, root `.gitignore`, `README.md` *(S)*
- [x] **1.2** Set up `docker-compose.yml` with PostgreSQL 15 + TimescaleDB extension, Redis 7, and a `backend` service placeholder *(S)*
- [x] **1.3** Scaffold FastAPI app: `app/main.py` (app factory), `app/config.py` (pydantic-settings), `app/dependencies.py` (empty Depends stubs) *(S)*
- [x] **1.4** Configure `pydantic-settings` for environment-based config; create `.env.example` with all required variables *(S)*
  - Use `environment-setup.md` as the canonical variable reference and copy the template from there
  - Confirm `.env` is in `.gitignore` before adding any real values to `.env`
  - *Depends on: 1.3*
- [x] **1.5** Initialize React 18 + TypeScript frontend with Tailwind CSS; verify dev server starts *(S)*
- [x] **1.6** Set up GitHub Actions CI: lint + test on push (Python: ruff + pytest; TS: tsc + eslint) *(M)*
  - *Depends on: 1.1*

**Checkpoint**: `docker-compose up` starts Postgres, Redis, and an empty FastAPI app. `GET /` returns 200.

---

### Layer 2 — Config & Logging

Set up structured logging before writing any business logic, so every layer that follows can log correctly from day one.

- [x] **2.1** Set up structured JSON logging (`app/monitoring/logger.py`): four named loggers (`trading`, `risk`, `system`, `error`), JSON format, configurable level via env *(M)*
  - Sensitive fields (account numbers, API keys) must be masked at the formatter level
- [x] **2.2** Configure `audit.log` as a **separate, append-only file handler** — not subject to rotation policy *(S)*
  - *Depends on: 2.1*
- [x] **2.3** Configure rotation for `trading.log`, `risk.log`, `system.log`, `error.log`: 10MB max, 5 backups *(S)*
  - *Depends on: 2.1*
- [x] **2.4** Add basic FastAPI error handling middleware (`app/api/middleware.py`) — catches unhandled exceptions, logs to `error.log`, returns standard error envelope *(S)*
  - *Depends on: 2.1, 1.3*

**Checkpoint**: Start the app, trigger an intentional error — see JSON log entries appear in the correct log file.

---

### Layer 3 — Database Layer

Build the full database layer (ORM → migrations → repositories) before wiring any API or business logic to it.

- [x] **3.1** Configure Alembic: `alembic/env.py` pointing at async SQLAlchemy engine, linked to `DATABASE_URL` env var *(S)*
  - *Depends on: 1.2, 1.3*
- [x] **3.2** Define SQLAlchemy ORM models (`app/db/models/`): `Trade`, `TradingStrategy`, `WatchedSymbol`, `SystemLog`, `PortfolioSnapshot` *(M)*
  - `WatchedSymbol`: `id` (UUID PK), `ticker` (VARCHAR 10, unique), `display_name`, `is_active`, `added_at`, `updated_at`
  - `Trade`: all columns from `design.md § trades` including `stop_loss_price` (required), `risk_amount`, `account_balance_at_entry`
  - `TradingStrategy`: `config` as JSONB (includes `symbols` array)
  - *Depends on: 3.1*
- [x] **3.3** Create Alembic initial migration from ORM models; verify `alembic upgrade head` creates all tables cleanly *(S)*
  - *Depends on: 3.2*
- [x] **3.4** Configure TimescaleDB hypertable for `portfolio_snapshots` (partition on `time` column) *(S)*
  - *Depends on: 3.3*
- [x] **3.5** Create async SQLAlchemy session factory (`app/db/session.py`) with connection pool (min 5, max 20) *(S)*
  - *Depends on: 3.2*
- [x] **3.6** Implement `SymbolRepo` (`app/db/repositories/symbol_repo.py`) — CRUD for `watched_symbols` *(S)*
  - *Depends on: 3.2, 3.5*
- [x] **3.7** Implement `TradeRepo` (`app/db/repositories/trade_repo.py`) — CRUD for `trades`; no update/delete on audit-sensitive fields *(M)*
  - *Depends on: 3.2, 3.5*
- [x] **3.8** Implement `StrategyRepo` (`app/db/repositories/strategy_repo.py`) — CRUD for `trading_strategies` *(M)*
  - *Depends on: 3.2, 3.5*
- [x] **3.9** Implement `PortfolioRepo` (`app/db/repositories/portfolio_repo.py`) — insert snapshots, query by time range *(S)*
  - *Depends on: 3.4, 3.5*

**Checkpoint**: Run migrations against the Docker Postgres instance. Use a DB client to confirm all tables and the hypertable exist. Write a quick pytest that inserts and retrieves a `WatchedSymbol` via the repo.

---

### Layer 4 — Broker Abstraction

> 📄 **Before starting this layer**, read `ibkr-gateway.md` for IB Gateway setup, authentication,
> and the daily startup procedure. Gateway must be running and authenticated before any task
> that uses `IBKRClient` can be tested against a real connection.
> Paper trading credentials should already be configured — see `ibkr-gateway.md § One-Time Configuration`.

Build the `MockBroker` first so all subsequent layers can be developed and tested without a live IBKR connection.

- [x] **4.1** Define `BaseBroker` abstract interface (`app/brokers/base.py`): `connect()`, `disconnect()`, `get_account_summary()`, `get_positions()`, `subscribe_price_feed(tickers)`, `place_order()`, `cancel_order()` *(S)*
- [x] **4.2** Implement `MockBroker` (`app/brokers/mock/client.py`): simulates connection, returns synthetic account/position data, echoes orders as filled *(M)*
  - *Depends on: 4.1*
- [x] **4.3** Implement `IBKRClient` (`app/brokers/ibkr/client.py`) wrapping `ib_async` *(L)*
  - `connect()` / `disconnect()` with connection status tracking
  - `get_account_summary()` → maps to internal `AccountSummary` model
  - `get_positions()` → maps to internal `Position` model
  - `subscribe_price_feed(tickers)` → attaches tick event handlers
  - `place_order()` / `cancel_order()`
  - *Depends on: 4.1*
- [x] **4.4** Implement `app/brokers/ibkr/mapper.py` — maps raw `ib_async` objects to internal Pydantic models *(S)*
  - *Depends on: 4.3*
- [x] **4.5** Wire broker selection via dependency injection: `get_broker()` in `app/dependencies.py` returns `IBKRClient` or `MockBroker` based on `ENVIRONMENT` env var *(S)*
  - *Depends on: 4.2, 4.3*

**Checkpoint A (no Gateway needed)**: Write a pytest using `MockBroker` that calls `connect()`, `get_account_summary()`, and `place_order()`. All pass without a live connection.

**Checkpoint B (Gateway required)**: With IB Gateway running in paper mode, run the sanity check script from `ibkr-gateway.md § Verifying Gateway Is Ready`. Confirm `IBKRClient` connects, retrieves account summary, and disconnects cleanly. Verify the pre-connection guard raises an error if `ENVIRONMENT=development` and `IBKR_TRADING_MODE=live` are both set.

---

### Layer 5 — Market Data

- [x] **5.1** Implement `RedisCache` wrapper (`app/data/cache.py`): `get`, `set`, `delete`, `publish`, `subscribe` helpers over `redis.asyncio` *(S)*
  - *Depends on: 1.2*
- [x] **5.2** Implement `MarketDataFeed` (`app/data/feed.py`): subscribes to live price ticks for all active `watched_symbols`, writes `price:{ticker}` to Redis, publishes to `watchlist_prices` pub/sub channel *(M)*
  - On symbol add/remove, update subscriptions dynamically without restart
  - *Depends on: 4.1, 5.1, 3.6*
- [x] **5.3** Implement `HistoricalDataFetcher` (`app/data/historical.py`): fetches 1-year OHLCV per symbol via broker, stores in TimescaleDB with overwrite-on-refresh policy *(L)*
  - *Depends on: 4.1, 3.4*

**Checkpoint** ✅: `docker-compose up` + `alembic upgrade head` (both migrations applied). 24 unit tests pass. 3 integration tests confirm: MockBroker + MarketDataFeed writes `price:{ticker}` to real Redis and publishes to `watchlist_prices`; HistoricalDataFetcher writes >200 OHLCV rows to the `ohlcv_bars` hypertable.

**Checkpoint**: Start the app with `MockBroker`. Confirm Redis receives `price:{ticker}` updates. Confirm historical fetch writes rows to TimescaleDB.

---

### Layer 6 — Risk Engine

The risk layer is a pure calculation and validation layer — no I/O, no DB calls. Build and fully test it before hooking up to execution.

- [x] **6.1** Define `BaseStrategy` abstract class (`app/core/strategy_engine/base.py`): `generate_signal()`, `calculate_position_size()`, `get_config_schema()` *(S)*
  - This is needed here only for the `RiskParams` type that `RiskCalculator` depends on
- [x] **6.2** Implement `RiskCalculator` (`app/core/risk/calculator.py`) *(M)*
  - Formula: `max_quantity = floor((account_balance × 0.01) / (entry_price − stop_loss_price))`
  - Raises `ValidationError` if `stop_loss_price >= entry_price`
  - Write unit tests: normal case, tiny stop distance, large account, fractional result, invalid stop (≥5 test cases)
  - *Depends on: 6.1*
- [x] **6.3** Implement `RiskManager.validate()` (`app/core/risk/manager.py`) — hard gate before order submission *(M)*
  - Reject immediately if no `stop_loss_price` provided
  - Reject if `risk_amount > 0.01 × account_balance_at_entry`
  - Snapshot `account_balance_at_entry` at time of validation
  - Log all rejections to `risk.log` with full context (symbol, qty, entry, stop-loss, risk amount, balance)
  - *Depends on: 6.2, 2.1*
- [x] **6.4** Implement `RiskMonitor` (`app/core/risk/monitor.py`): polls open trades, computes aggregate exposure, emits alerts at 75% and 90% of configurable thresholds *(M)*
  - *Depends on: 6.3, 3.7*

**Checkpoint** ✅: 14 `RiskCalculator` unit tests green (normal, tiny stop, large account, fractional floor, invalid stop × 4, risk_amount helpers). `RiskManager.validate()` with missing stop-loss raises `RiskRejectionError` and writes WARNING to `risk.log` — verified in `test_missing_stop_loss_is_logged`. All 5 `RiskMonitor` alert-level tests pass.

---

### Layer 7 — Trade Execution & Audit Trail ⚠️

This is the highest-criticality layer. The audit trail is non-optional (see `INF-04`).

- [x] **7.1** Implement `OrderManager.submit_order()` (`app/core/execution/order_manager.py`) *(L)*
  - **This is the only permitted code path for order submission** — enforced by architecture
  - **Pre-submission**: write audit entry to `audit.log` before broker call: trade ID, symbol, direction, qty, entry, stop-loss, risk amount, balance, strategy ID
  - **Post-confirmation**: write audit entry after broker responds: broker order ID, status, actual fill price/qty, error codes if any
  - If post-confirmation log write fails → escalate to `error.log` + fire system alert
  - Audit entries are append-only; no update/delete path permitted anywhere in the codebase
  - *Depends on: 6.3, 4.1, 2.2*
- [x] **7.2** Implement `TradeHandler` (`app/core/execution/trade_handler.py`): post-fill callback that persists trade to DB and publishes trade event to Redis *(M)*
  - *Depends on: 7.1, 3.7, 5.1*

**Checkpoint** ✅: Using MockBroker — (1) PRE_SUBMISSION audit entry written before broker call, (2) mock order placed and FILLED, (3) POST_CONFIRMATION entry written after fill, (4) trade row readable from DB via integration test, trade event published to `trade_events` Redis channel. Forced audit-write failure → `error_logger.critical` fired with trade_id and error detail. All 12 unit tests + 3 integration tests pass.

---

### Layer 8 — REST API & WebSocket

With all backend logic complete, expose it via thin FastAPI routes.

- [x] **8.1** Implement standard JSON response envelope and error format (`app/api/v1/schemas.py`) *(S)*
  - *Depends on: 1.3*
- [x] **8.2** Implement symbols endpoints (`app/api/v1/symbols.py`): `GET/POST /api/v1/symbols`, `DELETE /api/v1/symbols/{ticker}` *(M)*
  - `POST` validates ticker against broker before saving
  - `DELETE` with open position requires confirmation flag in request body
  - *Depends on: 3.6, 4.1, 8.1*
- [x] **8.3** Implement trades endpoints (`app/api/v1/trades.py`): `GET /api/v1/trades`, `GET /api/v1/trades/{id}` *(S)*
  - *Depends on: 3.7, 8.1*
- [x] **8.4** Implement strategies endpoints (`app/api/v1/strategies.py`): `GET /api/v1/strategies`, `PATCH /api/v1/strategies/{id}` *(M)*
  - `PATCH` handles both `is_enabled` toggle and JSONB config updates (including `config.symbols` assignment)
  - *Depends on: 3.8, 8.1*
- [x] **8.5** Implement portfolio/risk endpoint (`app/api/v1/portfolio.py`): `GET /api/v1/portfolio/risk` *(S)*
  - *Depends on: 3.9, 8.1*
- [x] **8.6** Implement system health endpoint (`app/api/v1/system.py`): `GET /api/v1/system/health` *(S)*
  - Returns broker, DB, and Redis status; HTTP 503 if any critical component down; response < 500ms
  - *Depends on: 4.1, 3.5, 5.1, 8.1*
- [x] **8.7** Implement WebSocket endpoint `/ws/dashboard` (`app/api/websocket.py`) *(L)*
  - Multiplexes three channels over a single connection: trade events, risk updates, `watchlist_prices`
  - Uses Redis pub/sub as the source of truth — WebSocket handler is a thin forwarder
  - *Depends on: 5.1, 5.2, 7.2*

**Checkpoint**: Use an HTTP client (curl or Postman) to hit every REST endpoint. Connect a WebSocket client to `/ws/dashboard` and confirm price updates stream in when `MarketDataFeed` is running.

---

## Phase 2 — Frontend

### Layer 9 — Frontend Shell

- [ ] **9.1** Build React app shell: page router, persistent nav/sidebar layout, placeholder routes for all pages *(M)*
  - Pages: Dashboard, Watchlist, Strategies, Portfolio, Symbol Detail, Backtesting, System Health
  - *Depends on: 1.5*
- [ ] **9.2** Set up React Query client and base API client (`src/api/client.ts`) with request/response type wrappers *(S)*
  - *Depends on: 9.1*
- [ ] **9.3** Set up WebSocket client hook (`src/hooks/useWebSocket.ts`) — connects to `/ws/dashboard`, reconnects on drop, exposes parsed event stream *(M)*
  - *Depends on: 9.1*

**Checkpoint**: App loads in browser, nav works, all pages show "coming soon" placeholders. No 404s or console errors.

---

### Layer 10 — Frontend Panels (Dashboard)

Build each panel separately, each wired to live data on completion.

- [ ] **10.1** Build `SystemHealthPanel` component — broker, DB, Redis status indicators; polls `GET /api/v1/system/health` every 30s *(S)*
  - *Depends on: 9.2, 8.6*
- [ ] **10.2** Build `ActiveTradesTable` component — symbol, direction, entry price, live P&L, duration; updates via WebSocket *(M)*
  - *Depends on: 9.3, 8.3*
- [ ] **10.3** Build `RiskGauge` component — aggregate exposure as % of account balance, color states (green/amber/red), updates via WebSocket *(M)*
  - *Depends on: 9.3, 8.5*
- [ ] **10.4** Build `WatchlistPanel` component — symbol rows with live price, day change %, strategy badge, position indicator; highlights rows with open positions; shows indicator on unassigned symbols *(M)*
  - Click row → navigate to `SymbolDetail` page
  - "Market Closed" indicator when outside hours
  - *Depends on: 9.3, 8.2*
- [ ] **10.5** Assemble Dashboard page from panels: `RiskGauge` (prominent), `ActiveTradesTable`, `WatchlistPanel`, `SystemHealthPanel` *(S)*
  - *Depends on: 10.1, 10.2, 10.3, 10.4*
- [ ] **10.6** Build `Watchlist` management page — add symbol (with inline broker validation error), remove symbol (confirm dialog if open position), list all symbols *(M)*
  - *Depends on: 9.2, 8.2*

**Checkpoint**: Full dashboard renders with live data from backend. Risk gauge updates when a mock trade fires. Watchlist panel rows update with price ticks.

---

## Phase 3 — Strategy Engine

### Layer 11 — Strategy Infrastructure

- [ ] **11.1** Implement `StrategyRegistry` (`app/core/strategy_engine/registry.py`) — register, enable/disable, and look up strategies at runtime *(M)*
  - *Depends on: 6.1, 3.8*
- [ ] **11.2** Implement strategy run scheduler (`app/core/strategy_engine/scheduler.py`) — poll or event-driven cycle; for each enabled strategy, iterates `config.symbols` and calls `generate_signal()` *(M)*
  - *Depends on: 11.1, 5.1*
- [ ] **11.3** Build strategy unit test framework with mock market data (`tests/unit/strategy/conftest.py`) *(M)*
  - *Depends on: 6.1*

---

### Layer 12 — First Strategy: Moving Average (50/200)

- [ ] **12.1** Implement `MovingAverageStrategy` (`app/core/strategy_engine/moving_average.py`) *(L)*
  - BUY signal: 50-day MA crosses above 200-day MA
  - SELL signal: 50-day MA crosses below 200-day MA
  - Configurable MA periods via JSONB config
  - *Depends on: 6.1, 5.3*
- [ ] **12.2** Register `MovingAverageStrategy` in `StrategyRegistry`; verify enable/disable toggle works without restart *(S)*
  - *Depends on: 12.1, 11.1*
- [ ] **12.3** Write unit tests for `MovingAverageStrategy` signal logic using mock market data *(M)*
  - *Depends on: 12.1, 11.3*

**Checkpoint**: Run the scheduler with `MockBroker` and a seeded historical dataset. Confirm the strategy fires signals, passes risk validation, and produces audit log entries.

---

### Layer 13 — Strategy UI

- [ ] **13.1** Build `StrategiesPage` — list all strategies with enable/disable toggle; toggle calls `PATCH /api/v1/strategies/{id}` *(M)*
  - *Depends on: 9.2, 8.4*
- [ ] **13.2** Build `StrategyConfigForm` — renders JSONB `config` fields as editable form inputs; includes multi-select of watchlist symbols for strategy assignment *(L)*
  - *Depends on: 13.1*
- [ ] **13.3** Build `StrategyPerformanceChart` — win rate, P&L per strategy, date range selector *(M)*
  - *Depends on: 13.1, 8.3*

---

## Phase 4 — Advanced Features

### Layer 14 — Risk Monitoring & Alerts

- [ ] **14.1** Wire `RiskMonitor` alerts to Redis pub/sub → WebSocket push to dashboard *(M)*
  - *Depends on: 6.4, 8.7*
- [ ] **14.2** Build `RiskMetricsPanel` — historical aggregate risk utilization chart for current trading day *(M)*
  - *Depends on: 14.1, 9.2*

---

### Layer 15 — Backtesting System

- [ ] **15.1** Implement `BacktestingEngine` (`app/core/backtesting/engine.py`) — replays signals against historical OHLCV with simulated risk-checked execution *(XL)*
  - *Depends on: 5.3, 6.1, 6.2*
- [ ] **15.2** Implement backtest result metrics: total return, win rate, max drawdown, Sharpe ratio *(L)*
  - *Depends on: 15.1*
- [ ] **15.3** Build `POST /api/v1/backtesting/run` async endpoint with status polling *(M)*
  - *Depends on: 15.1*
- [ ] **15.4** Build `BacktestingPage` — form to run backtest, results visualization *(L)*
  - *Depends on: 15.3, 9.1*

---

### Layer 16 — Performance Metrics & Notifications

- [ ] **16.1** Implement `MetricsCollector` — writes KPIs to TimescaleDB on trade events *(M)*
  - *Depends on: 3.9, 7.2*
- [ ] **16.2** Build `PerformanceDashboard` — KPI panel (win/loss ratio, total P&L, trade count, avg duration) with date range selector *(L)*
  - *Depends on: 16.1, 9.1*
- [ ] **16.3** Build `SystemMetricsPanel` — API latency, DB query latency, cache hit rate; updates every 30s *(M)*
  - *Depends on: 9.1*
- [ ] **16.4** Implement `NotificationDispatcher` with email delivery (SMTP) *(M)*
- [ ] **16.5** Integrate risk alerts and trade events with `NotificationDispatcher` *(M)*
  - *Depends on: 16.4, 6.4*

---

### Layer 17 — System Hardening

- [ ] **17.1** Implement broker reconnection with exponential backoff (max 5 attempts); log all retries at WARNING; fire system alert on exhaustion *(M)*
  - *Depends on: 4.3*
- [ ] **17.2** Audit and optimize slow DB queries; add indexes where needed *(M)*
- [ ] **17.3** Enable TimescaleDB chunk compression for `portfolio_snapshots` (7-day threshold) *(S)*
  - *Depends on: 16.1*
- [ ] **17.4** Add integration test suite for full trade execution flow end-to-end *(L)*
  - *Depends on: 7.1, 12.2*
- [ ] **17.5** Implement `MeanReversionStrategy` (`app/core/strategy_engine/mean_reversion.py`) *(L)*
  - *Depends on: 6.1, 5.3*
- [ ] **17.6** Implement `StockTrendStrategy` (stock vs 200-day MA) *(L)*
  - *Depends on: 6.1, 5.3*

---

## Phase 5 — Expansion & Refinement

- [ ] **18.1** Strategy combination framework — chain signal outputs between strategies *(XL)*
- [ ] **18.2** Advanced analytics: rolling Sharpe, drawdown charts, trade heatmaps *(L)*
- [ ] **18.3** Mobile notification delivery (push or SMS) *(M)*
- [ ] **18.4** Security hardening: API bound to localhost, secrets audit, HTTPS for webhooks *(M)*
- [ ] **18.5** Automated test coverage review — target ≥ 80% on core business logic *(L)*
- [ ] **18.6** Performance load test — simulate 5 strategies firing simultaneously *(M)*

---

## Phase 6 — Future Backlog

- [ ] Options trading strategy support (iron condor)
- [ ] Bull/bear market prediction strategy
- [ ] Intra-week mean reversion strategy
- [ ] Mobile PWA frontend
- [ ] Additional broker integration (abstraction layer ready)
- [ ] Machine learning signal integration
- [ ] Advanced backtesting: multi-strategy portfolio simulation

---

## Task Summary

| Layer | Name | Tasks |
|-------|------|-------|
| 1 | Project Skeleton | 6 |
| 2 | Config & Logging | 4 |
| 3 | Database Layer | 9 |
| 4 | Broker Abstraction | 5 |
| 5 | Market Data | 3 |
| 6 | Risk Engine | 4 |
| 7 | Trade Execution & Audit | 2 |
| 8 | REST API & WebSocket | 7 |
| 9 | Frontend Shell | 3 |
| 10 | Frontend Panels | 6 |
| 11 | Strategy Infrastructure | 3 |
| 12 | Moving Average Strategy | 3 |
| 13 | Strategy UI | 3 |
| 14 | Risk Monitoring & Alerts | 2 |
| 15 | Backtesting System | 4 |
| 16 | Performance & Notifications | 5 |
| 17 | System Hardening | 6 |
| 18 | Expansion | 6 |
| — | Backlog | 7 |
| **Total** | | **98** |
