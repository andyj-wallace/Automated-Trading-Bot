# design.md — System Architecture & Technical Design

> See `tech.md` for approved stack. See `structure.md` for file placement.

---

## System Architecture Overview

The system is composed of five primary layers communicating through well-defined interfaces. The backend is async-first (FastAPI + SQLAlchemy async), with Redis handling real-time pub/sub for live dashboard updates.

```mermaid
graph TD
    FE[React Frontend] -->|REST + WebSocket| API[FastAPI API Layer]
    API --> SE[Strategy Engine]
    API --> RM[Risk Manager]
    API --> DM[Data Manager]
    API --> NS[Notification Service]
    SE --> BK[Broker Abstraction Layer]
    RM --> BK
    BK --> IBKR[Interactive Brokers API]
    DM --> PG[(PostgreSQL + TimescaleDB)]
    DM --> RD[(Redis Cache)]
    SE --> PG
    RM --> PG
    NS --> EXT[Email / Mobile]
```

---

## Component Breakdown

### 1. API Layer (`backend/app/api/`)
- Thin FastAPI route handlers — no business logic
- Versioned under `/api/v1/`
- WebSocket endpoint at `/ws/dashboard` for real-time push
- Handles auth, request validation (Pydantic), and response formatting

### 2. Strategy Engine (`backend/app/core/strategy_engine/`)
- Executes registered strategies on a schedule or event trigger
- All strategies implement `BaseStrategy` abstract class
- Strategy registry allows runtime enable/disable without restart
- Supports strategy chaining: output signal of Strategy A can gate Strategy B

**Signal flow:**
```
Market Data → Strategy.generate_signal() → Signal
Signal + Portfolio State → RiskManager.validate() → Approved/Rejected
Approved Signal → OrderManager.execute() → Broker
```

### 3. Risk Management (`backend/app/core/risk/`)
- Enforces the **1% rule** as a hard gate on every individual trade (not configurable):
  - Maximum loss per trade = 1% of current account balance
  - Loss is defined as: `quantity × (entry_price − stop_loss_price)`
  - A stop-loss price is **required** for every trade — orders without one are rejected outright
  - Multiple trades can be open simultaneously; each independently capped at 1% (e.g. 5 trades = up to 5% total exposure)
- `RiskCalculator`: derives maximum safe position size from account balance, entry price, and stop-loss distance
- `RiskMonitor`: tracks aggregate open exposure across all active trades and emits alerts as total risk grows
- All trade executions must pass `RiskManager.validate()` before order submission

### 4. Broker Abstraction Layer (`backend/app/brokers/`)
- Abstract `BaseBroker` interface; all strategy and execution code depends on this, never on IBKR directly
- IBKR client (`ib_insync`) wrapped in `IBKRClient`
- `MockBroker` for paper trading and testing without live market connection

### 5. Data Management (`backend/app/data/`)
- `MarketDataFeed`: subscribes to live price data from broker
- `HistoricalDataFetcher`: pulls OHLCV data for backtesting (1-year lookback)
- Market data is **temporary** — stored with overwrite policy, not archived
- `RedisCache`: wraps all Redis operations; used for real-time metrics and pub/sub

### 6. Monitoring (`backend/app/monitoring/`)
- Structured JSON logging via Python `logging` module
- Four log streams: trading, risk, system, error (see `structure.md`)
- `MetricsCollector`: aggregates KPIs into TimescaleDB for historical analysis
- `AlertEngine`: evaluates alert rules and dispatches to `NotificationDispatcher`

### 7. Frontend Dashboard (`frontend/src/`)
- Real-time panels via WebSocket subscription to `/ws/dashboard`
- TradingView charts for price/indicator visualization
- React Query for REST polling (slower-changing data)
- Primary view: risk exposure gauge relative to 1% threshold

**Symbol & strategy management is entirely frontend-driven:**
- Users add/remove watchlist symbols via the UI → persisted to DB via REST
- Users assign strategies to symbols via the Strategy Config UI → stored in strategy JSONB `config.symbols`
- The backend has no hardcoded symbol list; it operates only on what the watchlist contains

**Key frontend pages and panels:**

| Page / Panel | Purpose |
|---|---|
| Dashboard | Risk gauge, active trades, watchlist panel, system status |
| Watchlist Panel | Live price, day change, strategy assignment, position status per symbol |
| Strategies Page | Enable/disable, parameter config, symbol assignment per strategy |
| Portfolio Page | Positions, P&L, trade history |
| Symbol Detail | Price chart, strategy signals, trade history for one symbol |
| Backtesting Page | Run backtests, view results |
| System Health | Metrics, logs viewer |

---

## Data Flow Diagrams

### Trade Execution Flow
```
[Scheduler / Signal Trigger]
        │
        ▼
[Strategy Engine: generate_signal()]
        │ Signal (BUY/SELL/HOLD + size hint)
        ▼
[Risk Manager: validate()]
    ├── REJECT → Log risk rejection → End
    └── APPROVE
            │
            ▼
    [Order Manager: submit_order()]
            │
            ▼
    [Broker Layer: place_order()]
            │
            ▼
    [IBKR API]
            │ Execution confirmation
            ▼
    [Trade Repo: record_trade()]
            │
            ▼
    [Redis Pub/Sub: broadcast to dashboard]
```

### Dashboard Real-time Update Flow
```
[Risk Monitor / Trade Handler]
        │ publishes event
        ▼
    [Redis Pub/Sub Channel: dashboard_updates]
        │
        ▼
    [FastAPI WebSocket Handler]
        │ pushes JSON message
        ▼
    [React Frontend WebSocket client]
        │
        ▼
    [React Query cache invalidation → UI re-render]
```

### Watchlist Price Feed Flow
```
[IBKR Real-time Price Feed]
        │ tick data per watched symbol
        ▼
[MarketDataFeed: on_price_update()]
        │ update Redis key: price:{ticker}
        ▼
    [Redis Pub/Sub: publish to watchlist_prices channel]
        │
        ▼
    [FastAPI WebSocket Handler]
        │ push { event: "price_update", ticker, price, day_change }
        ▼
    [WatchlistPanel React component re-renders row]
```

### Symbol–Strategy Resolution Flow (at signal generation)
```
[Strategy Scheduler: run cycle]
        │
        ▼
[StrategyRegistry: get enabled strategies]
        │ for each strategy
        ▼
[Read config.symbols from JSONB]
        │ for each assigned symbol
        ▼
[Fetch price data from Redis / TimescaleDB]
        │
        ▼
[Strategy.generate_signal(symbol, data)]
```

---

## Database Schema Design

### `watched_symbols`
| Column | Type | Notes |
|--------|------|-------|
| `id` | UUID PK | |
| `ticker` | VARCHAR(10) | Unique; e.g. `AAPL` |
| `display_name` | VARCHAR | e.g. `Apple Inc.` — populated on add |
| `is_active` | BOOLEAN | Soft disable without removing |
| `added_at` | TIMESTAMPTZ | |
| `updated_at` | TIMESTAMPTZ | |

### `trades`
| Column | Type | Notes |
|--------|------|-------|
| `id` | UUID PK | |
| `strategy_id` | UUID FK | → `trading_strategies` |
| `symbol` | VARCHAR(10) | Ticker symbol |
| `direction` | ENUM | `BUY`, `SELL` |
| `quantity` | DECIMAL | Sized to satisfy 1% rule |
| `entry_price` | DECIMAL | |
| `stop_loss_price` | DECIMAL | **Required** — used to calculate risk amount |
| `exit_price` | DECIMAL | Nullable until closed |
| `status` | ENUM | `OPEN`, `CLOSED`, `CANCELLED` |
| `risk_amount` | DECIMAL | `quantity × (entry_price − stop_loss_price)` — must be ≤ 1% of account balance at entry |
| `account_balance_at_entry` | DECIMAL | Snapshot of balance used for 1% calculation |
| `pnl` | DECIMAL | Nullable until closed |
| `executed_at` | TIMESTAMPTZ | |
| `closed_at` | TIMESTAMPTZ | Nullable |
| `created_at` | TIMESTAMPTZ | |

### `trading_strategies`
| Column | Type | Notes |
|--------|------|-------|
| `id` | UUID PK | |
| `name` | VARCHAR | |
| `type` | VARCHAR | e.g. `moving_average`, `mean_reversion` |
| `is_enabled` | BOOLEAN | |
| `config` | JSONB | Strategy parameters including `symbols` array — e.g. `{ "fast_period": 50, "slow_period": 200, "symbols": ["AAPL", "MSFT"] }` |
| `created_at` | TIMESTAMPTZ | |
| `updated_at` | TIMESTAMPTZ | |

### `portfolio_snapshots` (TimescaleDB hypertable)
| Column | Type | Notes |
|--------|------|-------|
| `time` | TIMESTAMPTZ | Partition key |
| `total_equity` | DECIMAL | |
| `cash_balance` | DECIMAL | |
| `open_position_value` | DECIMAL | |
| `open_trade_count` | INTEGER | Number of active trades |
| `aggregate_risk_amount` | DECIMAL | Sum of `risk_amount` across all open trades |
| `aggregate_risk_pct` | DECIMAL | `aggregate_risk_amount / account_balance` — e.g. 5 trades = ~5% |
| `max_per_trade_risk_pct` | DECIMAL | Should always be ≤ 1%; flags breaches if > 1% |

### `system_logs`
| Column | Type | Notes |
|--------|------|-------|
| `id` | BIGSERIAL PK | |
| `level` | VARCHAR | `INFO`, `WARNING`, `ERROR`, `CRITICAL` |
| `category` | ENUM | `TRADING`, `RISK`, `SYSTEM`, `ERROR` |
| `message` | TEXT | |
| `context` | JSONB | Structured contextual data |
| `created_at` | TIMESTAMPTZ | |

---

## API Design Patterns

- All endpoints return consistent envelope:
```json
{
  "data": { ... },
  "meta": { "timestamp": "...", "request_id": "..." },
  "error": null
}
```
- Errors return `4xx/5xx` with:
```json
{
  "data": null,
  "error": { "code": "RISK_LIMIT_EXCEEDED", "message": "..." }
}
```
- WebSocket messages follow:
```json
{
  "event": "trade_executed | risk_alert | position_update",
  "payload": { ... },
  "timestamp": "..."
}
```

---

## Performance Considerations

- **Async everywhere**: All I/O (DB, broker, Redis) uses async drivers (`asyncpg`, `redis.asyncio`)
- **Redis caching**: Current risk metrics and active positions cached; invalidated on state change
- **Batched metric writes**: Performance metrics buffered and written in batches to TimescaleDB
- **TimescaleDB compression**: Enable chunk compression on `portfolio_snapshots` after 7 days
- **Connection pooling**: SQLAlchemy async pool sized to expected concurrency (start: min 5, max 20)

---

## Audit Trail (Non-Negotiable Requirement)

Every trade execution must produce two mandatory log entries in `trading.log` — one before broker submission and one after confirmation (or failure). This is not optional and cannot be bypassed by any code path.

### Pre-Submission Entry (before order reaches broker)
Must include:
- Timestamp, trade ID, symbol, direction, quantity
- Entry price, stop-loss price, calculated risk amount
- Account balance at time of validation
- Strategy ID and signal that triggered the trade
- Risk validation result (always APPROVED at this stage — rejections never reach this step)

### Post-Confirmation Entry (after broker responds)
Must include:
- Timestamp, trade ID, broker order ID
- Execution status: `FILLED`, `PARTIAL`, `REJECTED`, `ERROR`
- Actual filled price and quantity (may differ from requested)
- Any broker error codes or messages
- Final recorded risk amount based on actual fill price

### Implementation Rules
- Both entries are written within the `OrderManager.submit_order()` execution path
- If the post-confirmation write fails, the error is escalated to `error.log` and a system alert is triggered — a missing post-confirmation entry is treated as a system fault
- Log entries are append-only and must never be modified after writing
- Audit logs are retained separately from rotating application logs (no overwrite policy)

---

## Security Considerations

- All broker API credentials stored in environment variables only — never in DB or source
- Sensitive fields (account numbers, API keys) masked in all log output
- No user authentication required (personal-use system), but API should bind to localhost or VPN only in production
- HTTPS enforced for any external notification webhook delivery

---

> See `requirements.md` for feature user stories.
> See `tasks.md` for implementation task breakdown.
