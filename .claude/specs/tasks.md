# tasks.md — Implementation Task Tracker

> Status: `[ ]` todo · `[~]` in progress · `[x]` done
> Effort: S = ~2–4hrs · M = ~1 day · L = ~2–3 days · XL = 3+ days

> See `design.md` for architecture. See `requirements.md` for acceptance criteria.

---

## Phase 1 — Core Infrastructure (Weeks 1–6)

### Week 1 — Project Setup & Basic Architecture

- [ ] **1.1** Initialize monorepo structure with `backend/` and `frontend/` directories *(S)*
- [ ] **1.2** Set up `docker-compose.yml` with PostgreSQL, TimescaleDB, and Redis services *(S)*
- [ ] **1.3** Scaffold FastAPI app with `app/main.py`, `config.py`, `dependencies.py` *(S)*
- [ ] **1.4** Configure `pydantic-settings` for environment-based config (`.env.example`) *(S)*
- [ ] **1.5** Initialize React + TypeScript frontend with Tailwind CSS *(S)*
- [ ] **1.6** Set up GitHub Actions CI pipeline (lint + test on push) *(M)*
- [ ] **1.7** Configure Alembic for database migrations *(S)*
  - *Depends on: 1.2, 1.3*

---

### Week 2 — Data Infrastructure

- [ ] **2.1** Define SQLAlchemy ORM models: `Trade`, `TradingStrategy`, `WatchedSymbol`, `SystemLog` *(M)*
  - `WatchedSymbol`: `id`, `ticker`, `display_name`, `is_active`, `added_at`, `updated_at`
  - *Depends on: 1.7*
- [ ] **2.2** Create Alembic initial migration from ORM models *(S)*
  - *Depends on: 2.1*
- [ ] **2.3** Configure TimescaleDB hypertable for `portfolio_snapshots` *(S)*
  - *Depends on: 2.2*
- [ ] **2.4** Implement `IBKRClient` wrapper around `ib_insync` *(L)*
  - `connect()`, `disconnect()`, `get_account_summary()`, `get_positions()`, `subscribe_price_feed(tickers)`
- [ ] **2.5** Implement `MockBroker` for development/testing *(M)*
  - *Depends on: 2.4 (shares `BaseBroker` interface)*
- [ ] **2.6** Implement `HistoricalDataFetcher` — fetch OHLCV via IBKR, store in TimescaleDB *(L)*
  - *Depends on: 2.4, 2.3*
- [ ] **2.7** Implement `RedisCache` wrapper with get/set/pub/sub helpers *(S)*
  - *Depends on: 1.2*
- [ ] **2.8** Implement `SymbolRepo` — CRUD for `watched_symbols` table *(S)*
  - *Depends on: 2.1*
- [ ] **2.9** Implement `MarketDataFeed` — subscribe to live prices for all active watchlist symbols, write to Redis *(M)*
  - On symbol add/remove, update subscriptions dynamically without restart
  - *Depends on: 2.4, 2.7, 2.8*

---

### Week 3–4 — Basic Trading Engine

- [ ] **3.1** Define `BaseStrategy` abstract class with `generate_signal()` and `calculate_position_size()` *(S)*
- [ ] **3.2** Implement `RiskCalculator` — stop-loss-based 1% position sizing *(M)*
  - Formula: `max_quantity = floor((account_balance × 0.01) / (entry_price − stop_loss_price))`
  - Reject if `stop_loss_price >= entry_price`
  - Write unit tests: normal case, tiny stop distance, large account, fractional result, invalid stop
  - *Depends on: 3.1*
- [ ] **3.3** Implement `RiskManager.validate()` — hard gate before order submission *(M)*
  - Reject immediately if no `stop_loss_price` provided
  - Reject if `risk_amount > 0.01 × account_balance_at_entry`
  - Snapshot `account_balance_at_entry` at time of validation
  - Log all rejections to `risk.log` with full context (symbol, qty, entry, stop-loss, risk amount, balance)
  - *Depends on: 3.2*
- [ ] **3.4** Implement `OrderManager.submit_order()` with mandatory audit trail *(L)* ⚠️
  - **Pre-submission**: write audit entry to `trading.log` before broker call (trade ID, symbol, direction, qty, entry, stop-loss, risk amount, balance, strategy ID)
  - **Post-confirmation**: write audit entry after broker responds (broker order ID, status, actual fill price/qty, error codes if any)
  - If post-confirmation log write fails → escalate to `error.log` + fire system alert
  - Audit entries are append-only; no update/delete path permitted
  - `submit_order()` is the **only** permitted code path for order submission — enforce via architecture, not convention
  - *Depends on: 3.3, 2.4, 3.7*
- [ ] **3.4a** Configure separate append-only audit log file (`audit.log`) *(S)*
  - Not subject to 10MB rotation/overwrite policy
  - Retained indefinitely (manual archival only)
  - *Depends on: 3.7*
- [ ] **3.5** Implement trade repository (`TradeRepo`) — CRUD for `trades` table *(M)*
  - *Depends on: 2.1*
- [ ] **3.6** Implement strategy repository (`StrategyRepo`) — CRUD for `trading_strategies` *(M)*
  - *Depends on: 2.1*
- [ ] **3.7** Set up structured logging — four log streams, JSON format, rotation config *(M)*
  - `trading.log`, `risk.log`, `system.log`, `error.log`
- [ ] **3.8** Implement basic error handling middleware in FastAPI *(S)*
  - *Depends on: 1.3*

---

### Week 5–6 — Simple Web Interface

- [ ] **4.1** Create FastAPI REST endpoints: trades, strategies, portfolio, system health, symbols *(L)*
  - `GET/POST /api/v1/trades`, `GET/PATCH /api/v1/strategies`
  - `GET/POST/DELETE /api/v1/symbols` — watchlist CRUD with broker ticker validation on add
  - `GET /api/v1/system/health`
  - *Depends on: 3.5, 3.6, 2.8*
- [ ] **4.2** Implement WebSocket endpoint `/ws/dashboard` with Redis pub/sub integration *(L)*
  - Multiplex: trade events, risk updates, and `watchlist_prices` channel into single WS connection
  - *Depends on: 2.7, 2.9, 4.1*
- [ ] **4.3** Build React dashboard shell: page routing, layout, nav *(M)*
- [ ] **4.4** Build `ActiveTradesTable` component (symbol, direction, P&L, duration) *(M)*
  - *Depends on: 4.1, 4.3*
- [ ] **4.5** Build `RiskGauge` component — exposure as % of 1% limit, color states *(M)*
  - *Depends on: 4.2, 4.3*
- [ ] **4.6** Build `SystemHealthPanel` — broker, DB, Redis status indicators *(S)*
  - *Depends on: 4.1, 4.3*
- [ ] **4.7** Build `WatchlistPanel` component — symbol rows with live price, day change, strategy badge, position indicator *(M)*
  - Highlight rows with open positions
  - Show indicator on symbols with no strategy assigned
  - Click row → navigate to `SymbolDetail` page
  - *Depends on: 4.2, 4.3*
- [ ] **4.8** Build `Watchlist` management page — add/remove symbols, assign strategies to symbols *(M)*
  - Validate ticker against broker on add; show error for invalid tickers
  - Confirm dialog when removing a symbol with an open position
  - *Depends on: 4.1, 4.3*
- [ ] **4.9** Connect React frontend to WebSocket for live updates across all dashboard panels *(M)*
  - *Depends on: 4.2, 4.4, 4.5, 4.7*

---

## Phase 2 — Strategy Implementation (Weeks 7–11)

### Weeks 7–8 — First Strategy + Testing Framework

- [ ] **5.1** Implement `MovingAverageStrategy` (50/200 crossover) *(L)*
  - *Depends on: 3.1, 2.6*
- [ ] **5.2** Register strategy in `StrategyRegistry` with enable/disable support *(M)*
  - *Depends on: 5.1, 3.6*
- [ ] **5.3** Implement strategy run scheduler (poll or event-driven) *(M)*
  - *Depends on: 5.2*
- [ ] **5.4** Build strategy unit test framework with mock market data *(M)*
  - *Depends on: 3.1*
- [ ] **5.5** Write unit tests for `MovingAverageStrategy` signal logic *(M)*
  - *Depends on: 5.1, 5.4*

---

### Weeks 8–9 — Risk Management System

- [ ] **6.1** Implement `RiskMonitor` — poll open positions, compute aggregate exposure *(M)*
  - *Depends on: 3.2, 3.5*
- [ ] **6.2** Implement risk alert rules (75% and 90% threshold triggers) *(M)*
  - *Depends on: 6.1*
- [ ] **6.3** Integrate risk alerts with Redis pub/sub → WebSocket push *(M)*
  - *Depends on: 6.2, 4.2*
- [ ] **6.4** Build `RiskMetricsPanel` — historical risk utilization chart for today *(M)*
  - *Depends on: 6.1, 4.3*

---

### Weeks 9–11 — Strategy Management Interface

- [ ] **7.1** Build `StrategiesPage` — list all strategies with enable/disable toggle *(M)*
  - *Depends on: 4.1, 4.3*
- [ ] **7.2** Build `StrategyConfigForm` — render JSONB config as editable form *(L)*
  - *Depends on: 7.1*
- [ ] **7.3** Build `StrategyPerformanceChart` — win rate, P&L per strategy *(M)*
  - *Depends on: 7.1, 4.1*
- [ ] **7.4** Implement `PATCH /api/v1/strategies/{id}` for config and enable/disable *(S)*
  - *Depends on: 3.6, 4.1*

---

## Phase 3 — Advanced Features (Weeks 12–19)

### Weeks 12–14 — Backtesting System

- [ ] **8.1** Implement `BacktestingEngine` — replay signals against historical OHLCV *(XL)*
  - *Depends on: 2.6, 3.1, 3.2*
- [ ] **8.2** Implement backtest result metrics: return, win rate, max drawdown, Sharpe *(L)*
  - *Depends on: 8.1*
- [ ] **8.3** Build `POST /api/v1/backtesting/run` async endpoint with status polling *(M)*
  - *Depends on: 8.1*
- [ ] **8.4** Build `BacktestingPage` with results visualization *(L)*
  - *Depends on: 8.3, 4.3*

---

### Weeks 14–16 — Enhanced Monitoring

- [ ] **9.1** Implement `MetricsCollector` — write KPIs to TimescaleDB on trade events *(M)*
  - *Depends on: 3.5, 2.3*
- [ ] **9.2** Build `PerformanceDashboard` — KPI panel with date range selector *(L)*
  - *Depends on: 9.1, 4.3*
- [ ] **9.3** Build `SystemMetricsPanel` — API latency, DB latency, cache hit rate *(M)*
  - *Depends on: 4.3*
- [ ] **9.4** Implement `NotificationDispatcher` with email delivery *(M)*
- [ ] **9.5** Integrate risk alerts and trade events with `NotificationDispatcher` *(M)*
  - *Depends on: 9.4, 6.2*

---

### Weeks 16–19 — System Optimization

- [ ] **10.1** Audit and optimize slow DB queries — add indexes where needed *(M)*
- [ ] **10.2** Enable TimescaleDB chunk compression for `portfolio_snapshots` (7-day threshold) *(S)*
  - *Depends on: 9.1*
- [ ] **10.3** Implement broker reconnection with exponential backoff *(M)*
  - *Depends on: 2.4*
- [ ] **10.4** Add integration test suite for trade execution flow end-to-end *(L)*
  - *Depends on: 3.4, 5.2*
- [ ] **10.5** Add `MeanReversionStrategy` implementation *(L)*
  - *Depends on: 3.1, 2.6*
- [ ] **10.6** Add `StockTrendStrategy` (stock vs 200-day MA) *(L)*
  - *Depends on: 3.1, 2.6*

---

## Phase 4 — Expansion & Refinement (Weeks 20–25)

- [ ] **11.1** Strategy combination framework — chain signal outputs between strategies *(XL)*
- [ ] **11.2** Advanced analytics: rolling Sharpe, drawdown charts, trade heatmaps *(L)*
- [ ] **11.3** Mobile notification delivery (push or SMS) *(M)*
- [ ] **11.4** Security hardening: API bound to localhost, secrets audit, HTTPS for webhooks *(M)*
- [ ] **11.5** Automated test coverage review — target ≥ 80% on core business logic *(L)*
- [ ] **11.6** Performance load test — simulate 5 strategies firing simultaneously *(M)*

---

## Phase 5 — Future (Backlog)

- [ ] Options trading strategy support (iron condor)
- [ ] Bull/bear market prediction strategy
- [ ] Intra-week mean reversion strategy
- [ ] Mobile PWA frontend
- [ ] Additional broker integration (abstraction layer ready)
- [ ] Machine learning signal integration
- [ ] Advanced backtesting: multi-strategy portfolio simulation

---

## Task Summary

| Phase | Tasks | Status |
|-------|-------|--------|
| Phase 1 — Core Infrastructure | 24 | 0 / 24 done |
| Phase 2 — Strategy Implementation | 15 | 0 / 15 done |
| Phase 3 — Advanced Features | 16 | 0 / 16 done |
| Phase 4 — Expansion | 6 | 0 / 6 done |
| Phase 5 — Backlog | 7 | Not started |
| **Total** | **68** | **0 / 61 active** |
