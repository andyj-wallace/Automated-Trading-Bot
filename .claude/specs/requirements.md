# requirements.md — User Stories & Acceptance Criteria

> See `design.md` for architecture context. See `tasks.md` for implementation breakdown.

---

## Feature Areas

1. [Core Infrastructure](#1-core-infrastructure)
2. [Symbol & Strategy Management](#2-symbol--strategy-management)
3. [Risk Management](#3-risk-management)
4. [Monitoring & Dashboard](#4-monitoring--dashboard)
5. [Backtesting](#5-backtesting)
6. [Notifications](#6-notifications)
7. [System Health](#7-system-health)

> ⚠️ **Non-optional requirement**: See `INF-04 — Trade Audit Trail`. This requirement has no bypass and is enforced at the infrastructure level. See `design.md § Audit Trail` for full specification.

---

## Priority Levels
- **P0** — Blocking; system cannot operate safely without this
- **P1** — Core functionality; required for Phase 1–2 completion
- **P2** — Important but deferrable to Phase 3+

---

## 1. Core Infrastructure

### INF-01 — Database Setup
**Priority**: P0

> As a developer, I want a working PostgreSQL + TimescaleDB database with migrations, so that all trading data is persisted reliably.

**Acceptance Criteria:**
- [ ] PostgreSQL and TimescaleDB run via Docker Compose
- [ ] Alembic migration creates all required tables on first run
- [ ] TimescaleDB hypertable configured for `portfolio_snapshots`
- [ ] Redis instance available for caching and pub/sub
- [ ] Connection health check endpoint returns status

---

### INF-02 — Broker Integration (IBKR)
**Priority**: P0

> As a trader, I want the system connected to Interactive Brokers, so that I can execute trades programmatically.

**Acceptance Criteria:**
- [ ] `IBKRClient` connects to IBKR TWS or Gateway on startup
- [ ] Connection status exposed via `/api/v1/system/health`
- [ ] Reconnection logic handles dropped connections automatically
- [ ] `MockBroker` is available for testing without live connection
- [ ] All broker calls go through `BaseBroker` interface — no direct IBKR calls in strategy code

---

### INF-03 — Structured Logging
**Priority**: P1

> As a developer, I want structured JSON logs for all system events, so that I can diagnose issues quickly.

**Acceptance Criteria:**
- [ ] Four log streams configured: `trading`, `risk`, `system`, `error`
- [ ] All logs include: timestamp, level, category, message, and context dict
- [ ] Sensitive data (account numbers, API keys) masked in all log output
- [ ] Log rotation: 10MB per file, 5 backup files retained
- [ ] Log level configurable via environment variable

---

### INF-04 — Trade Execution Audit Trail ⚠️ NON-OPTIONAL
**Priority**: P0

> As a trader, I need every trade execution logged in full before and after broker confirmation, so that there is a complete, tamper-proof record of every order the system has ever placed or attempted.

**This requirement has no bypass.** Any code path that submits an order must produce both log entries. A missing entry is treated as a system fault.

**Acceptance Criteria:**
- [ ] **Pre-submission entry** written to `trading.log` before the order reaches the broker, containing: timestamp, trade ID, symbol, direction, quantity, entry price, stop-loss price, calculated risk amount, account balance at validation, strategy ID, and risk validation result
- [ ] **Post-confirmation entry** written to `trading.log` after broker responds, containing: timestamp, trade ID, broker order ID, execution status (`FILLED` / `PARTIAL` / `REJECTED` / `ERROR`), actual filled price and quantity, any broker error codes
- [ ] If the post-confirmation write itself fails, an error is escalated to `error.log` and a system alert fires — a missing post-confirmation entry is a system fault, not an acceptable gap
- [ ] Audit log entries are append-only; no update or delete path exists in the codebase
- [ ] Audit logs are retained separately from rotating application logs and are **not subject to the 10MB rotation/overwrite policy**
- [ ] Both entries are produced inside `OrderManager.submit_order()` — this is the single enforced choke point; no other code path may submit orders
- [ ] Pre-submission entry includes: `take_profit_price`, `reward_to_risk_ratio`, `submit_stop_to_broker` flag
- [ ] Close entries (from `PositionMonitor` triggers) are also written to `trading.log` with `exit_reason`

---

### INF-05 — Trade Lifecycle State Tracking
**Priority**: P0

> As a trader, I want every trade to have a visible status reflecting exactly where it is in its lifecycle, so that I can see at a glance whether an order is pending, live, being closed, or done.

**Status definitions:**
| Status | Meaning |
|--------|---------|
| `PENDING` | Risk approved; trade row created; pre-submission audit written; order not yet sent to broker |
| `SUBMITTED` | Entry order sent to broker; awaiting fill confirmation |
| `OPEN` | Broker confirmed fill (`FILLED` or `PARTIAL`); `PositionMonitor` active |
| `CLOSING` | Stop or target hit; close order submitted; awaiting broker confirmation |
| `CLOSED` | Close confirmed; `exit_price`, `exit_reason`, `pnl` written |
| `CANCELLED` | Order rejected or cancelled before reaching `OPEN` (only reachable from `PENDING` or `SUBMITTED`) |

**Acceptance Criteria:**
- [ ] `trades.status` column is an ENUM with values: `PENDING`, `SUBMITTED`, `OPEN`, `CLOSING`, `CLOSED`, `CANCELLED`
- [ ] `OrderManager.submit_order()` creates the trade row at `PENDING` before any broker call
- [ ] Status transitions to `SUBMITTED` immediately before `place_order()` is called
- [ ] Status transitions to `OPEN` on broker `FILLED` or `PARTIAL` response
- [ ] Status transitions to `CANCELLED` on broker `REJECTED` or `ERROR` response (from `PENDING` or `SUBMITTED` only)
- [ ] `PositionMonitor` sets status to `CLOSING` when it submits a close order
- [ ] Status transitions to `CLOSED` on close order broker confirmation; `exit_price`, `exit_reason`, `pnl` written atomically
- [ ] No backward transitions permitted — any unexpected transition is logged as `CRITICAL` to `error.log`
- [ ] All status transitions are reflected in the dashboard via Redis pub/sub within 5 seconds

---

## 2. Symbol & Strategy Management

### SYM-01 — Watchlist Management
**Priority**: P1

> As a trader, I want to add and remove symbols from a watchlist via the frontend, so that the system only tracks and trades securities I have explicitly chosen.

**Acceptance Criteria:**
- [ ] Watchlist page (or panel) allows adding a symbol by ticker (e.g. `AAPL`)
- [ ] Symbol is validated against broker before saving — invalid or unsupported tickers are rejected with a clear error
- [ ] Symbols can be removed from the watchlist; removing a symbol with an open position requires explicit confirmation
- [ ] Maximum of 10 symbols enforced in the UI (initial scale target is 5, hard cap is 10)
- [ ] Watchlist persisted in the database — survives system restarts
- [ ] `GET /api/v1/symbols` and `POST/DELETE /api/v1/symbols/{ticker}` endpoints back the UI

---

### SYM-02 — Symbol–Strategy Assignment
**Priority**: P1

> As a trader, I want to assign one or more strategies to each watchlist symbol via the frontend, so that I control exactly which strategies run on which securities.

**Acceptance Criteria:**
- [ ] Strategy configuration UI includes a multi-select of watchlist symbols per strategy (stored in JSONB `config.symbols`)
- [ ] A strategy only generates signals for its assigned symbols — no implicit "run on all" behaviour
- [ ] A symbol can be assigned to multiple strategies simultaneously
- [ ] Removing a symbol from the watchlist automatically unassigns it from all strategies
- [ ] Assignment changes take effect on the next strategy run cycle without a restart

---

### STR-01 — Moving Average Strategy (50/200)
**Priority**: P1

> As a trader, I want the 50 vs 200 day moving average strategy implemented and runnable, so that I can automate my primary trend-following approach.

**Acceptance Criteria:**
- [ ] Strategy generates BUY signal when 50-day MA crosses above 200-day MA
- [ ] Strategy generates SELL signal when 50-day MA crosses below 200-day MA
- [ ] Signal includes calculated position size (pre-risk-check)
- [ ] Strategy is configurable via JSONB config (e.g., MA periods)
- [ ] Signals are logged to `trading.log` with full context

---

### STR-02 — Strategy Enable/Disable
**Priority**: P1

> As a trader, I want to enable or disable individual strategies without restarting the system, so that I can respond to market conditions quickly.

**Acceptance Criteria:**
- [ ] `PATCH /api/v1/strategies/{id}` accepts `{ "is_enabled": true/false }`
- [ ] Disabled strategy does not generate signals or execute trades
- [ ] State change is persisted in database and reflected immediately
- [ ] UI toggle updates strategy status with < 2 second feedback

---

### STR-03 — Strategy Configuration Interface
**Priority**: P1

> As a trader, I want a visual UI to configure strategy parameters, so that I don't need to edit code or config files to adjust a strategy.

**Acceptance Criteria:**
- [ ] Strategy config page displays all JSONB parameters as form inputs
- [ ] Parameter changes are validated before saving
- [ ] Changes take effect on the next strategy run cycle
- [ ] Config history is not required (current config only)

---

### STR-04 — Mean Reversion Strategy
**Priority**: P2

> As a trader, I want a mean reversion strategy implemented, so that I can capture short-term price deviations from historical averages.

**Acceptance Criteria:**
- [ ] Strategy computes rolling mean and standard deviation over configurable lookback
- [ ] BUY signal generated when price falls below mean by configurable threshold
- [ ] SELL signal generated when price reverts to mean
- [ ] All signals pass through same risk validation as other strategies

---

### STR-05 — Strategy Performance Comparison
**Priority**: P2

> As a trader, I want to compare performance metrics across strategies, so that I can allocate to the best-performing ones.

**Acceptance Criteria:**
- [ ] Dashboard shows win rate, P&L, and trade count per strategy
- [ ] Date range filter available (default: 30 days)
- [ ] Metrics calculated from closed trades in database

---

## 3. Risk Management

### RSK-01 — 1% Rule Enforcement (Per Trade)
**Priority**: P0

> As a trader, I want the system to hard-block any trade that would risk more than 1% of my account balance, so that no single trade can cause a loss greater than 1% of my capital.

**Rule Definition:**
- The 1% rule applies **per individual trade**, not portfolio-wide
- Risk per trade = `quantity × (entry_price − stop_loss_price)`
- Multiple trades may be open simultaneously — each independently capped at 1%
- Example: 5 concurrent trades each risking 1% = 5% total open exposure (this is valid)
- A **stop-loss price is mandatory** on every trade — no stop-loss means automatic rejection

**Acceptance Criteria:**
- [ ] `RiskManager.validate()` called before every order submission
- [ ] Validation requires a `stop_loss_price` field — missing stop-loss rejects the trade immediately
- [ ] Risk calculation: `risk_amount = quantity × (entry_price − stop_loss_price)`
- [ ] Trade is hard-rejected (not warned) if `risk_amount > 0.01 × account_balance_at_entry`
- [ ] `account_balance_at_entry` is snapshotted at time of validation, not recalculated later
- [ ] Rejection logged to `risk.log` with: symbol, quantity, entry, stop-loss, calculated risk amount, account balance, and % exceeded
- [ ] Rejected trades never reach the broker layer

---

### RSK-02 — Stop-Loss-Based Position Size Calculator
**Priority**: P0

> As a trader, I want the system to calculate the correct position size from my entry price and stop-loss price, so that if the stop-loss is hit, I lose exactly 1% (or less) of my account balance — never more.

**Formula:**
```
max_quantity = floor( (account_balance × 0.01) / (entry_price − stop_loss_price) )
```

**Acceptance Criteria:**
- [ ] `RiskCalculator.max_quantity(account_balance, entry_price, stop_loss_price)` returns max safe quantity
- [ ] Result always rounds **down** to whole shares (never up)
- [ ] Function raises a validation error if `stop_loss_price >= entry_price` (invalid stop placement)
- [ ] Result respects any additional per-strategy position size caps (whichever is smaller wins)
- [ ] Calculation is unit tested with at least 5 cases: normal, tiny stop distance, large account, fractional result, invalid stop

---

### RSK-03 — Stop-Loss Ownership & Strategy Suggestions
**Priority**: P0

> As a trader, I want the risk engine to own stop-loss determination so that strategies cannot bypass risk rules by setting their own stop prices.

**Rule:**
- The risk engine is the sole authority on the stop-loss price used for position sizing and trade validation
- A strategy may include a `stop_loss_price` suggestion in its signal; the risk engine validates it (must be < entry, must produce a sensible stop distance) and accepts or rejects it
- If no suggestion is provided, the risk engine calculates the stop-loss via a configured default method

**Acceptance Criteria:**
- [ ] `Signal.stop_loss_price` is optional — strategies may suggest but never mandate a stop
- [ ] `RiskManager.validate()` validates any suggested stop; rejects if invalid (e.g. stop ≥ entry)
- [ ] Approved stop-loss is included in `ValidationResult` and persisted on the `trades` record
- [ ] Stop-loss is always stored internally regardless of whether it is sent to the broker

---

### RSK-04 — Reward-to-Risk Ratio Enforcement
**Priority**: P0

> As a trader, I want every trade to meet a minimum reward-to-risk ratio so that winners are structurally larger than losers, making the system profitable even with a sub-50% win rate.

**Rule:**
- Minimum R:R ratio is configurable (default 2:1); no trade may be entered below the minimum
- `required_take_profit = entry_price + (stop_distance × min_rr_ratio)`
- A strategy may suggest a `take_profit_price`; it is accepted only if ≥ `required_take_profit`
- If no suggestion is provided, the risk engine sets `take_profit_price = required_take_profit`
- Trades where the minimum R:R is unachievable are rejected

**Acceptance Criteria:**
- [ ] `RiskManager` has a configurable `min_reward_to_risk: Decimal` (default `2.0`)
- [ ] Every approved trade has a `take_profit_price` set in `ValidationResult`
- [ ] `Signal.take_profit_price` is optional — strategy suggestion accepted if it meets minimum R:R
- [ ] Trade rejected with reason `INSUFFICIENT_REWARD` if suggested take-profit is below required target
- [ ] Actual `reward_to_risk_ratio` stored on the `trades` record at entry
- [ ] Rejection logged to `risk.log` with: symbol, entry, stop, required target, suggested target (if any)

---

### RSK-05 — Portfolio Max Risk Enforcement
**Priority**: P0

> As a trader, I want a hard cap on total portfolio exposure so that even when multiple trades are open simultaneously, my total capital at risk stays within a defined limit.

**Rule:**
- Maximum aggregate open exposure: configurable, default 5%, hard system ceiling 10%
- The ceiling of 10% is enforced in code and cannot be overridden via configuration
- A new trade is rejected if `current_aggregate_risk + new_risk_amount > max_portfolio_risk_pct × account_balance`

**Acceptance Criteria:**
- [ ] `RiskManager.validate()` receives `current_aggregate_risk: Decimal` as a parameter
- [ ] Trade rejected with reason `PORTFOLIO_RISK_LIMIT_EXCEEDED` if aggregate would exceed configured max
- [ ] `MAX_PORTFOLIO_RISK_HARD_LIMIT = Decimal("0.10")` defined as a non-overridable constant
- [ ] Default `max_aggregate_risk_pct = Decimal("0.05")` — configurable up to but not exceeding 0.10
- [ ] `RiskMonitorConfig` validates that `max_aggregate_risk_pct ≤ MAX_PORTFOLIO_RISK_HARD_LIMIT` on instantiation
- [ ] Rejection logged to `risk.log` with: current aggregate exposure, new trade risk, configured limit

---

### RSK-06 — Internal Position Monitoring (Stop & Target)
**Priority**: P0

> As a trader, I want the system to automatically close positions when the stop-loss or take-profit price is hit, without relying on broker-side stop orders.

**Rule:**
- `PositionMonitor` subscribes to `price:{ticker}` in Redis for all open positions
- When `price ≤ stop_loss_price`: trigger `OrderManager.close_position(reason=STOP_LOSS)`
- When `price ≥ take_profit_price`: trigger `OrderManager.close_position(reason=TAKE_PROFIT)`
- `exit_reason` is recorded on the `trades` record on close

**Acceptance Criteria:**
- [ ] `PositionMonitor` (`app/core/execution/position_monitor.py`) subscribes to Redis price feed for all open trades
- [ ] Stop-loss trigger closes position and records `exit_reason=STOP_LOSS`
- [ ] Take-profit trigger closes position and records `exit_reason=TAKE_PROFIT`
- [ ] Manual close records `exit_reason=MANUAL`
- [ ] `PositionMonitor` runs as a background task alongside the main app
- [ ] Position close events are published to Redis pub/sub for dashboard update

---

### RSK-07 — Optional Broker Stop-Loss Submission
**Priority**: P1

> As a trader, I want to optionally send a stop-loss order to the broker so that my position has downside protection even if the system goes offline.

**Rule:**
- Each strategy can set `submit_stop_to_broker: bool` on its signal (default `false`)
- When `true`, `OrderManager` includes the stop-loss as a native stop order in the broker request
- Take-profit is always managed internally — never sent to the broker

**Acceptance Criteria:**
- [ ] `Signal.submit_stop_to_broker: bool = False` field on base signal model
- [ ] `TradeRequest.submit_stop_to_broker: bool = False` forwarded to `OrderManager`
- [ ] `OrderManager` conditionally sets `stop_loss_price` on `OrderRequest` based on the flag
- [ ] Stop-loss is always stored in the `trades` record regardless of the flag
- [ ] Broker stop submission is noted in the pre-submission audit entry

---

### RSK-08 — Real-time Risk Monitoring
**Priority**: P1

> As a trader, I want to see both per-trade risk and total aggregate exposure across all open trades on the dashboard, so that I understand both individual trade risk and my overall capital at risk.

**Acceptance Criteria:**
- [ ] Dashboard displays per-trade risk: each open trade shows its locked-in risk amount and % of account balance
- [ ] Dashboard displays aggregate exposure: sum of all open trade risk amounts as % of account balance (e.g. "3 trades open = 3.0% total at risk")
- [ ] Dashboard shows take-profit target and R:R ratio per open trade
- [ ] All metrics update within 5 seconds of any position change
- [ ] Historical aggregate risk visible as a chart for the current trading day

---

### RSK-09 — Risk Threshold Alerts
**Priority**: P1

> As a trader, I want alerts when total open exposure reaches notable levels, so that I maintain awareness of my capital at risk.

**Acceptance Criteria:**
- [ ] Alert fires when total aggregate open exposure exceeds configurable thresholds (default: WARNING at 75% of max, CRITICAL at 90% of max)
- [ ] Aggregate exposure alerts are **informational only** — they do not block new trades (blocking happens at Gate 4 in `RiskManager.validate()`)
- [ ] Alerts delivered via dashboard notification and configured channels
- [ ] Each alert includes: trigger reason, current aggregate exposure, configured limit, open trade count

---

## 4. Monitoring & Dashboard

### MON-01 — Real-time Dashboard
**Priority**: P1

> As a trader, I want a real-time dashboard showing active trades, risk metrics, watchlist prices, and system status, so that I have full visibility during market hours.

**Acceptance Criteria:**
- [ ] Dashboard loads within 3 seconds
- [ ] Active trades table: symbol, direction, entry price, current P&L, open duration
- [ ] Risk panel: current exposure gauge (primary focus, prominently placed)
- [ ] Watchlist panel: live price and day change for all configured symbols (see MON-04)
- [ ] System status indicators: broker connection, DB, Redis
- [ ] All panels update via WebSocket without page refresh

---

### MON-02 — Trading Performance KPIs
**Priority**: P1

> As a trader, I want to see key trading metrics on the dashboard, so that I can evaluate my system's performance at a glance.

**Acceptance Criteria:**
- [ ] Metrics displayed: win/loss ratio, total P&L, trade count, avg position duration
- [ ] Metrics scoped to: today, this week, this month (tab selector)
- [ ] P&L shown as both dollar amount and percentage of starting equity
- [ ] Breakdown available per strategy

---

### MON-03 — System Metrics Panel
**Priority**: P2

> As a developer, I want system health metrics visible in the dashboard, so that I can detect performance degradation before it causes trading issues.

**Acceptance Criteria:**
- [ ] Metrics displayed: API response times, DB query latency, cache hit rate, memory usage
- [ ] Metrics update every 30 seconds
- [ ] Warning indicator when any metric exceeds defined threshold

---

### MON-04 — Watchlist Panel
**Priority**: P1

> As a trader, I want a watchlist panel on the dashboard showing the current price and status of every symbol I'm tracking, so that I have market context alongside my active trades and risk metrics.

**Panel columns:**
| Column | Description |
|--------|-------------|
| Symbol | Ticker (e.g. `AAPL`) |
| Last Price | Most recent trade price |
| Day Change | $ and % change from previous close |
| Strategy | Active strategy name(s) assigned to this symbol |
| Position | `LONG` / `SHORT` / `—` (no open position) |
| Risk Allocated | Risk amount of open position, if any |

**Acceptance Criteria:**
- [ ] Panel displays all symbols currently in the watchlist — no symbols outside the watchlist appear
- [ ] Price and day change data sourced from broker real-time feed and pushed via WebSocket
- [ ] Panel updates within 5 seconds of a price change during market hours
- [ ] Symbols with an open position are visually distinguished (e.g. highlighted row)
- [ ] Symbols with no assigned strategy are shown with a visual indicator prompting assignment
- [ ] Clicking a symbol row navigates to that symbol's detail view (trades, strategy signals, price chart)
- [ ] Outside market hours, last known price is displayed with a "Market Closed" indicator

---

## 5. Backtesting

### BT-01 — Historical Data Management
**Priority**: P2

> As a trader, I want to fetch and store up to 1 year of historical OHLCV data per symbol, so that I have enough data to backtest strategies meaningfully.

**Acceptance Criteria:**
- [ ] Historical data fetched via IBKR API on demand
- [ ] Data stored in TimescaleDB with overwrite-on-refresh policy
- [ ] Minimum 252 trading days (1 year) of data available per symbol
- [ ] Data freshness timestamp visible in UI

---

### BT-02 — Backtest Execution
**Priority**: P2

> As a trader, I want to run a backtest of any strategy against historical data, so that I can validate performance before trading live.

**Acceptance Criteria:**
- [ ] `POST /api/v1/backtesting/run` accepts strategy ID, symbol, and date range
- [ ] Backtest simulates signal generation and risk-checked position sizing
- [ ] Results include: total return, win rate, max drawdown, Sharpe ratio
- [ ] Results stored and retrievable via GET endpoint
- [ ] Backtest runs asynchronously; status polled or pushed via WebSocket

---

## 6. Notifications

### NOT-01 — Trade Execution Alerts
**Priority**: P1

> As a trader, I want to be notified immediately when a trade is executed, so that I'm always aware of position changes.

**Acceptance Criteria:**
- [ ] Notification sent within 30 seconds of trade execution
- [ ] Notification includes: symbol, direction, quantity, price, strategy name
- [ ] Delivered to at least one configured channel (email or mobile)

---

### NOT-02 — Multi-channel Notification Delivery
**Priority**: P2

> As a trader, I want notifications delivered via email and mobile, so that I receive alerts even when away from my desk.

**Acceptance Criteria:**
- [ ] Email delivery configured via SMTP settings
- [ ] Mobile delivery mechanism defined (push or SMS)
- [ ] Fallback: if primary channel fails, attempt secondary channel
- [ ] Notification delivery status logged

---

## 7. System Health

### SYS-01 — Health Check Endpoint
**Priority**: P0

> As a developer, I want a system health endpoint, so that I can verify all components are operational.

**Acceptance Criteria:**
- [ ] `GET /api/v1/system/health` returns status of: API, DB, Redis, broker connection
- [ ] Returns HTTP 200 if all healthy, HTTP 503 if any critical component is down
- [ ] Response time < 500ms

---

### SYS-02 — Error Recovery
**Priority**: P1

> As a trader, I want the system to recover from transient API and connection failures automatically, so that temporary outages don't require manual intervention.

**Acceptance Criteria:**
- [ ] Broker connection retries with exponential backoff (max 5 attempts)
- [ ] DB connection pool recovers from dropped connections automatically
- [ ] Redis reconnects automatically on failure
- [ ] All retry attempts logged at WARNING level
- [ ] System alert sent if recovery fails after max retries
