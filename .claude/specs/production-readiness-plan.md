# production-readiness-plan.md — Phase 7: Make It Work, Make It Safe, Ship It

> **Audience:** the agent implementing this in a fresh session, with no memory of the review that produced it.
> Everything you need is in this file or linked from it. Read §0 completely before changing any code.
>
> **Created:** 2026-10-19 from a full review of `backend/` and `frontend/` (backtesting internals excluded).
> **Supersedes:** the uncommitted "8. Hardening" list in `trading-bot-masterplan.md` (mapped in §1.4).
> **Line numbers** in evidence columns are from 2026-10-19 and may drift; search for the quoted symbol if they do.

---

## 0. How to work through this plan

### 0.1 Order and scope

- Do phases in order: **A → B → C → D → E → F → G**, then optional **Z**. Inside a phase, do tasks in the listed order unless a task says *(parallel-safe)*.
- Every task has: **Fixes** (finding IDs from §1.2), **Files**, **Steps**, **Pattern** (reference code: adapt names, keep the semantics), **Tests**, **Done when**.
- **Minimum before leaving the bot running unattended on the paper account:** A1–A4, B1–B9, C1–C5, C7, C9, D1, D2, D4, D6, D7, F1–F4, G1.
- **Additionally required before any live-money use:** C6 (broker protective stop), C8 (single-instance lock), and the user's explicit go-ahead after G1.
- Out of scope: backtesting internals (except D7), short selling, options, switching to live trading.

### 0.2 Commands

Backend lint and unit tests (run after every task):

```bash
cd backend
ruff check .
python -m pytest tests/unit -q
```

Integration tests need Postgres and Redis. One-time setup:

```bash
docker compose up -d db redis
docker compose exec db createdb -U trading trading_bot_test
```

Every integration run (**always export DATABASE_URL first** — until task A4 lands, the integration tests default to the *dev* database and `TRUNCATE` its `trades` table):

```bash
cd backend
export DATABASE_URL=postgresql+asyncpg://trading:trading@localhost:5432/trading_bot_test
export REDIS_URL=redis://localhost:6379/1
export SECRET_KEY=local-test-secret-key-0123456789abcdef0123456789
export ENVIRONMENT=development
alembic upgrade head
python -m pytest tests/integration -q
```

Frontend (after every frontend task):

```bash
cd frontend
npm run typecheck && npm run lint && npm run build
```

### 0.3 Definition of done (every task)

1. `ruff check .` clean, unit tests green; integration tests green if you touched DB, Redis, broker, or execution code; frontend checks green if you touched `frontend/`.
2. New behavior is covered by tests (unit, plus integration when it crosses DB/Redis).
3. Tick the task checkbox in this file (`- [x]`). If you deviated from the plan, add one line under the task saying how and why.
4. Commit only if the user has asked for commits: one commit per task, short lowercase imperative message in the repo's style (e.g. `wire market data feed into engine`).

### 0.4 Guardrails: never do these

- Never set `IBKR_TRADING_MODE=live`, never connect to Gateway ports 4001/4003 (live), never change the Gateway container to `TRADING_MODE: live`.
- Never read, print, or copy values from `.env`, `backend/.env`, `.env.prod`, or `credentials.txt`. Use `.env.example` for key names.
- Never modify audit-immutable trade columns after creation: `entry_price`, `stop_loss_price`, `take_profit_price`, `reward_to_risk_ratio`, `risk_amount`, `account_balance_at_entry`. Record fills in the new separate columns (task C1).
- Never make the 1% per-trade rule configurable. Never allow the portfolio cap above 10%.
- Never implement short selling. Never send take-profit orders to the broker (requirement RSK-07: take-profit stays internal).
- Never run more than one uvicorn worker or more than one backend replica.
- Never use `float` for money in the backend; build `Decimal` from `str`.
- Never call `asyncio.create_task(...)` without keeping a reference. Use `spawn()` / `PeriodicTask` from task A3.
- Never use a reserved `LogRecord` attribute as a key in `extra={...}`: `message`, `asctime`, `msg`, `args`, `name`, `levelname`, `levelno`, `pathname`, `filename`, `module`, `lineno`, `funcName`, `created`, `msecs`, `relativeCreated`, `thread`, `threadName`, `process`, `processName`, `exc_info`, `exc_text`, `stack_info`, `taskName`. Python raises `KeyError` and the log call (and everything after it) never runs.
- Never add dependencies other than `exchange_calendars==4.13.2` and `tzdata==2026.5` (backend). No new frontend packages.
- Never delete or rewrite `logs/audit.log` content; it is append-only.

### 0.5 Not bugs: do not "fix" these

- `CircuitBreaker.record_closed_trade()` needs no lock. It is synchronous, and asyncio cannot interleave inside a synchronous call. (masterplan hardening item 1)
- A break-even close (`pnl == 0`) resetting the consecutive-loss streak is intended. (item 9)
- The DB query inside `OrderManager._submission_lock` is intentional; it is what makes the portfolio cap race-free. (item 5)

### 0.6 Codebase conventions you must follow

- Routes stay thin; logic lives in `app/core`; database access only through `app/db/repositories`; responses use `ok()` / `err()` from `app/api/v1/schemas.py`.
- Money and prices are `Decimal`; serialize them as strings in JSON.
- Store and compare times as timezone-aware UTC `datetime`. Market-hours logic only through `app/core/market_calendar.py` (task A2).
- Log with `trading_logger`, `risk_logger`, `system_logger`, `error_logger`, `audit_logger` from `app.monitoring.logger`, always with structured `extra={...}`.
- Anything that depends on "now" or market hours takes injectable callables so tests can control time:
  `clock: Callable[[], datetime] = market_calendar.now_et` and `is_market_open: Callable[[], bool] = market_calendar.is_market_open`.
- Tests: `pytest.ini` sets `asyncio_mode = auto`. Reuse existing fakes: `FakeCache` and `FakeSymbolRepo` in `tests/unit/data/test_feed.py`; `FakeCache` in `tests/unit/execution/test_trade_handler.py`; `FakeTradeRepo`/`FakeSession` in `tests/unit/risk/test_manager.py`. Integration fixtures and helpers to reuse: `tests/integration/test_layer17_e2e.py` lines 73–209 (`broker`, `cache`, `engine`, `session_factory`, `_make_order_manager`, `_open_trade`, `_poll_for_status`).
- Migrations are hand-written: `backend/alembic/versions/000N_<name>.py` with `revision = "000N"` and `down_revision` = previous. Copy the style of `0003_6b_enhancements.py` (enum `ADD VALUE` inside `op.get_context().autocommit_block()`) and `0004_performance_indexes.py`. **Next free number: 0007.** Always write a working `downgrade()`.
- No comments that restate what code does. One short docstring line per public function or class is enough.

---

## 1. Review findings (2026-10-19)

### 1.1 Bottom line

The components are well built and well unit-tested in isolation, but **the live trading loop is not connected**:

- no price feed is ever started;
- the stop/target monitor listens on Redis channels nobody publishes to;
- new trades are never announced to the monitor;
- strategies re-fire the same signal every 60 seconds on bars that are never refreshed.

If strategies were enabled today, the bot would buy the same symbol every minute until the 5% portfolio cap stopped it, and would **never close anything**. This has not happened only because no strategy rows exist: all 260 audit-log entries are test "AAPL" trades. The deployment files are development-only.

### 1.2 Findings

Severity: **Crit** = trading is broken or unsafe · **High** = must fix before unattended running · **Med** = should fix · **Low** = cleanup.

| ID | Sev | Finding | Evidence | Task |
|----|-----|---------|----------|------|
| F1 | Crit | `MarketDataFeed` is never constructed or started. No live ticks reach Redis, the position monitor, or the UI. | `app/main.py:80-206`, `app/data/feed.py` | B3 |
| F2 | Crit | Stops and targets never fire. `PositionMonitor` pattern-subscribes to `price:*` channels; nothing publishes there (the feed SETs a *key* `price:X` and publishes to `watchlist_prices`). The integration test publishes to `price:TSLA` itself, which hides the bug. | `position_monitor.py:183-189`, `feed.py:122-124`, `tests/integration/test_layer17_e2e.py:639` | B3, B4, B9 |
| F3 | Crit | `trade_executed` is never published: `TradeHandler.on_fill()` is only called from tests. The monitor ignores new trades until a restart; UI and notifications never hear about fills. | `order_manager.py:88-90`, `scheduler.py:308-309` | B5 |
| F4 | Crit | Repeated entries. Signals are recomputed every 60 s from the same stored bars, `MarketData.open_position` is never set, and nothing deduplicates. All crossover strategies re-fire while the last stored bar is a crossover; the state-based ones (bull/bear inverse-ETF leg, intra-week VWAP) fire every cycle by design. | `scheduler.py:253-309`, `strategy_engine/base.py:49`, `intra_week_reversion.py:112` | B7 |
| F5 | Crit | Daily bars only refresh when someone clicks "fetch history". Strategies evaluate stale data indefinitely. | `api/v1/symbols.py:122-143` (only caller of `HistoricalDataFetcher`) | B6 |
| F6 | Crit | No code path creates strategy rows (no seed, no POST endpoint). A fresh deployment cannot trade. | `api/v1/strategies.py`, `alembic/versions/0006_seed_watchlist.py` | B8 |
| F7 | Crit | An order still working at IBKR after 30 s (`Submitted`/`PreSubmitted`) maps to `PARTIAL` and is treated as filled: entries become OPEN at full size; closes become CLOSED with exit price 0 and pnl 0 while the sell is still pending (e.g. a stop "hit" by an extended-hours tick queues a market order until the open). Unfilled orders are never cancelled. | `brokers/ibkr/mapper.py:103-111`, `order_manager.py:213-217,387-394`, `brokers/ibkr/client.py:337-342` | C2, C4 |
| F8 | Crit | Order retries can double-submit: `place_order` is retried on any exception with no idempotency key (an exception after IBKR accepted the order sends a second one). | `order_manager.py:257-295` | C3 |
| F9 | Crit | IBKR connection: only 5 reconnect attempts over ~155 s, then it gives up forever (IB's nightly restart can last ~90 min). No retry if the first connect at startup fails. Market data is not re-requested after reconnect (`ib_async` clears its tickers, and the client's own map turns re-subscribe into a no-op). | `client.py:53-54,105-122,156-241,264-288` | B2 |
| F10 | Crit | Bad ticks: `ib_async` uses NaN, not `None`, for missing values, so NaN prices flow downstream (Decimal comparisons with NaN raise). Before the first trade print, the previous day's close is used as the live price. | `client.py:290-313` | B1 |
| F11 | Crit | No reconciliation between DB and broker: orphan positions, trades stuck in SUBMITTED/CLOSING, and positions closed outside the app are never detected. | — | C5 |
| F12 | Crit | `close_position` reads OPEN then writes CLOSING without compare-and-set. Two concurrent closers (monitor + manual + reconciler) can send two sells, creating an accidental short. | `order_manager.py:321-353` | C4 |
| F13 | Crit | Closes sell the *requested* quantity, not the filled quantity, and PnL uses the planned entry price instead of the fill price. | `order_manager.py:344-347,391-394` | C1, C4 |
| F14 | Crit | `submit_stop_to_broker` (requirement RSK-07) is ignored by `IBKRClient.place_order`. Positions have zero protection while the app, feed, or Gateway is down. | `client.py:319-342`, `order_manager.py:164-175` | C6 |
| F15 | Crit | Background loops die silently: the monitor's and the notifier's Redis loops exit permanently on one connection error; fire-and-forget tasks can be garbage-collected mid-flight. | `position_monitor.py:183-208,277,348`, `main.py:183-204`, `client.py:120,313` | A3, B4, D4 |
| F16 | High | The daily-loss halt never auto-lifts (`is_halted()` ignores the date; rollover only happens when another trade closes), and all circuit-breaker state is lost on restart (a crash or redeploy lifts halts and kill switches). | `core/risk/circuit_breaker.py:105-119` | C7 |
| F17 | High | No market-hours awareness: the scheduler runs 24/7 and the monitor acts on extended-hours ticks. | `scheduler.py:115-127`, `position_monitor.py:214-277` | A2, B4, B7 |
| F18 | High | When the price cache is empty, the scheduler requests IBKR historical data every cycle for every symbol (price and bars fallbacks), which triggers IB pacing violations. | `scheduler.py:338-368,405-413` | B7 |
| F19 | High | No operator controls: no pause/resume, no manual close (an unmet RSK-06 acceptance criterion), no flatten, no manual order; circuit-breaker endpoints have no UI. | `api/v1/*`, `.claude/specs/requirements.md:334` | D1–D3, E4 |
| F20 | High | Tests are not hermetic. `TestClient(app)` runs the real lifespan (real DB/Redis from `.env`; starts scheduler and monitor; with `BROKER=ibkr` in the container env it connects to the Gateway) and writes to the real `backend/logs/audit.log`. Integration tests default to the **dev** database and `TRUNCATE trades`. | `tests/conftest.py`, `tests/unit/api/test_layer8.py:105-108`, `test_layer17_e2e.py:62-66,116-130` | A4 |
| F21 | High | All engine state lives in one process with no single-instance guard. Two instances (redeploy overlap, `--workers 2`) would double-trade. | `app/main.py` | C8 |
| F22 | High | Dashboard live updates are broken. Price events don't match the UI contract (no `event` field, `ticker` vs `symbol`, `day_change_pct` vs `change_pct`, string vs number); one shared `lastEvent` lets fast ticks overwrite trade events; the active-trades table hides PENDING/SUBMITTED/CLOSING; `TradeStatus` has 3 of 6 values. | `feed.py:122-124`, `WatchlistPanel.tsx:49-61`, `useWebSocket.ts:48-56`, `ActiveTradesTable.tsx:23-29`, `types/api.ts:48` | B3, E1–E3 |
| F23 | High | Deployment files are development-only: compose uses `--reload` with a source mount, hardcodes `ENVIRONMENT=development`, publishes Postgres/Redis/API on all interfaces, has no Gateway or frontend services and no restart policies. The backend image copies `backend/.env` (no `.dockerignore`) and runs as root. The frontend nginx has no `/api`/`/ws` proxy and no SPA fallback. CORS is hardcoded to the Vite dev URL. | `docker-compose.yml`, `backend/Dockerfile`, `frontend/Dockerfile`, `main.py:241-247` | F1–F3 |
| F24 | High | The paper/live guard compares env strings only; it never checks which account the Gateway is actually logged into. | `client.py:414-429` | B2 |
| F25 | High | Alert coverage is minimal: only "reconnect exhausted" exists; nothing for halts, kills, failed closes, stale feed, or mismatches. `error.log` only receives `error_logger` calls, so most errors (logged through `system_logger.error`) never reach it. | `monitoring/logger.py:263-268` | D4 |
| F26 | High | System-alert emails/SMS are never sent and IB informational messages are never logged: `extra={"message": ...}` raises `KeyError` (reserved key) before delivery. `eventkit` swallows the IB one silently. | `notifications/dispatcher.py:209`, `client.py:152` | A3 |
| F27 | Med | Aggregate-risk statuses disagree: OrderManager counts PENDING/SUBMITTED/OPEN, RiskMonitor and MetricsCollector count OPEN only, and nothing counts CLOSING (still at risk). | `order_manager.py:238-255`, `risk/monitor.py:154-159`, `metrics/collector.py:63-64` | C9 |
| F28 | Med | Risk and engine parameters are hardcoded constructor defaults (5% cap, 2:1, 3% daily loss, 5 losses, 14 days, 60 s). | `risk/manager.py:124-128`, `circuit_breaker.py:33-34`, `position_monitor.py:96-100`, `scheduler.py:50` | A1 |
| F29 | Med | `PATCH /strategies` stores invalid configs (the scheduler then fails every cycle); strategy config schemas are duplicated in the frontend. | `strategy_repo.py:95-117`, `StrategyConfigForm.tsx` | B8, E5 |
| F30 | Med | Two `RedisCache` instances (lifespan and dependency singleton); only one is closed on shutdown. | `main.py:119`, `dependencies.py:30-34` | A4 |
| F31 | Med | Backtests run CPU-bound on the trading event loop, stalling stop checks while they run. | `api/v1/backtesting.py:164,258,320-375` | D7 |
| F32 | Med | Each `subscribe_price_feed` call appends the callback again, so every tick is processed N times. | `client.py:278`, `feed.py:85` | B1 |
| F33 | Med | Portfolio snapshots are only taken on trade events, so Sharpe/drawdown are computed on irregular samples. | `metrics/collector.py` | D5 |
| F34 | Med | The SymbolDetail page is a placeholder and signal history is not stored anywhere. | `frontend/src/pages/SymbolDetail.tsx` | B7, E6 |
| F35 | Med | Doc drift: specs say Gateway port 4001 for paper (paper is 4002; the Docker image uses 4004), and say `ENVIRONMENT` selects the broker (it is `BROKER`). `env.example` is a stale duplicate of `.env.example`. | `ibkr-gateway.md:113,153`, `environment-setup.md:115,154` | A1, F6 |
| F36 | Med | The trailing-stop level lives only in memory; a restart reverts to the original stop. | `position_monitor.py:18-24` | Z2 |
| F37 | Low | Leftover hardening items: duplicated `min_separation_pct` parsing; sequential extra-symbol fetch; `min_separation_ok` passes when the slow MA ≤ 0; composite can emit a signal with no stop. | strategy modules | Z1 |
| F38 | Low | Unused `system_logs` table; test dependencies in the runtime image; stray root script `test_ibkr_connection.py`. | — | Z3 |

### 1.3 What is solid: keep it

- `OrderManager` as the single order path, with pre/post audit entries and the PENDING-reservation lock (the portfolio cap is race-free).
- `RiskManager` gates (1% rule hardcoded, R:R, portfolio cap, circuit-breaker gate) and `RiskCalculator` math.
- Decimal discipline, the repository pattern, structured JSON logging with masking.
- Strategy implementations, the registry, and the whipsaw filter.
- 518 passing unit tests; CI with real Postgres and Redis.

### 1.4 Masterplan "8. Hardening" list → this plan

| # | Item | Disposition |
|---|------|-------------|
| 1 | CircuitBreaker race | Not a bug (§0.5) |
| 2 | close failures skip the breaker | Reframed: record PnL only when a close actually completes (C4, C5); failed closes alert and get retried |
| 3 | fire-and-forget close tasks | A3, B4 |
| 4 | silently skipped `trade_executed` | B4 (periodic DB resync replaces event-driven tracking) |
| 5 | DB query inside lock | Intentional (§0.5) |
| 6 | `min_separation_pct` duplication | Z1 |
| 7 | two retry layers | B2 (one reconnect supervisor) + C3 (idempotent order retry) |
| 8 | sequential extra-symbol fetch | Z1 |
| 9 | break-even resets streak | Intended (§0.5) |
| 10 | `min_separation_ok` degenerate | Z1 |
| 11 | composite `None` stop | Z1 |
| 12 | RiskMonitor statuses | C9 |
| 13 | composite majority single pass | Skip (cosmetic) |

### 1.5 Already resolved on 2026-10-19

- `credentials.txt` was purged from git history and is gitignored.
- `backend/.env` now uses `IBKR_PORT=4002` (paper).

---

## 2. Decisions (confirm with the user at session start; use the defaults if they don't answer)

| ID | Decision | Default | Why |
|----|----------|---------|-----|
| D1 | Hosting | Any always-on host running `docker-compose.prod.yml` (a Linux VM, or the user's Mac). Nothing is exposed publicly; the user reaches the UI through an SSH tunnel or Tailscale. AWS is optional (F7). | Personal system; `design.md` says bind to localhost/VPN only. |
| D2 | Broker protective stop | **On** (`BROKER_PROTECTIVE_STOPS=true`): after every fill, a GTC stop order sits at IBKR at the risk-approved stop price. The internal monitor stays the primary exit; take-profit stays internal. | Without it, any outage during market hours leaves positions unprotected. |
| D3 | When strategies are evaluated | NYSE trading days, **09:45–15:45 ET**, every 60 s. Each (strategy, symbol, bar date, action) acts at most once. | Avoids opening-auction noise; signals come from completed daily bars. |
| D4 | Positions per symbol | **One active trade per symbol** across all strategies. | Prevents stacking risk on one name; makes reconciliation exact. |
| D5 | Market data type | Live (`IBKR_MARKET_DATA_TYPE=1`). If the paper account lacks a US-equities data subscription (IB errors 354, 10089 or 10167), use `3` (delayed); paper only, never live. | Delayed data makes stops 15 min late. |
| D6 | Acting outside regular hours | Stops, targets, stale-trade closes and manual closes act **only while the market is open**. An overnight gap fills at the next open. | Market orders outside regular hours just queue until the open. |

---

## 3. Target architecture

### 3.1 Runtime components (one backend process, one uvicorn worker)

```
                      ┌────────────────────────────── TradingEngine (app/core/engine.py) ───────────────────────────────┐
 IB Gateway (paper) ◄─┤ IBKRClient ── supervisor: connect / reconnect forever / re-subscribe / paper-account guard        │
                      │    │ ticks (sanitized)                                                                          │
                      │    ▼                                                                                            │
                      │ MarketDataFeed ──► in-process listeners ──► PositionMonitor ──► OrderManager.close_position()    │
                      │    │   ├─ Redis SET  price:{SYM} = {"price","timestamp"}  (read by StrategyScheduler)    ▲       │
                      │    │   └─ Redis PUB  watchlist_prices  (price_update envelope → WebSocket → UI)          │       │
                      │    ▲ sync() every 60 s: watchlist ∪ enabled-strategy symbols ∪ active-trade symbols     │       │
                      │                                                                                         │       │
                      │ StrategyScheduler (D3 window) ── completed DB bars + fresh price key ── strategy ──► claim signal (DB unique)
                      │                                                                     └──► OrderManager.submit_order()
                      │ Reconciler: startup, after reconnect, every 120 s ── broker positions/orders by ref ⇄ DB trades
                      │ BarRefreshJob: 2-year backfill at startup, 10-day refresh 16:30 ET   DailySummaryJob: 16:45 ET
                      │ RiskMonitor: 30 s     EngineLock: 10 s renew     Notifier: supervised Redis subscriber
                      └─────────────────────────────────────────────────────────────────────────────────────────────────┘
 Postgres/Timescale: trades, strategy_signals, ohlcv_bars, portfolio_snapshots, trading_strategies, watched_symbols
 Redis: price keys, pub/sub channels, trading:paused, trading_engine:lock
```

### 3.2 Flows

- **Price:** IB tick → `IBKRClient._on_pending_tickers` (drop NaN/≤0, last-trade price only) → `MarketDataFeed._on_price_update` → (1) in-process listeners first (`PositionMonitor.on_price_update`), (2) Redis key `price:{SYM}`, (3) publish `price_update` on `watchlist_prices`.
- **Entry:** scheduler cycle inside the D3 window → completed daily bars (DB) + fresh price (Redis key, ≤ `PRICE_STALE_SECONDS` old) → `generate_signal` → claim `(strategy, symbol, bar_date, action)` in `strategy_signals` → `OrderManager.submit_order` (under lock: one-active-trade-per-symbol check + aggregate risk → `RiskManager.validate` → PENDING row) → market order with `orderRef="<trade_id>:entry"` → fill recorded → OPEN → protective stop `"<trade_id>:stop"` → `trade_executed` published.
- **Exit:** monitor hit (market open) / stale sweep / manual API → `close_position` (atomic OPEN→CLOSING, stores intended `exit_reason`) → cancel protective stop → market sell of the *filled* quantity with `orderRef="<trade_id>:exit"` → PnL from fill prices → breaker → CLOSED → `trade_closed` published.
- **Reconcile:** at startup, after every reconnect, and every 120 s: compare broker positions and orders-by-ref with DB active trades; resolve SUBMITTED/CLOSING, finalize positions closed outside the app, alert on anything unexplained.

### 3.3 Event contract (single source of truth: `app/core/events.py`)

| Channel | `event` | `payload` | Producer | Consumers |
|---------|---------|-----------|----------|-----------|
| `watchlist_prices` | `price_update` | `{symbol, price: str, change_pct: str \| null}` | MarketDataFeed | WebSocket → UI |
| `trade_events` | `trade_executed` | existing `TradeHandler` payload (`trade_id, symbol, direction, quantity, entry_price, stop_loss_price, take_profit_price, risk_amount, reward_to_risk_ratio, status`) | OrderManager (via TradeHandler) | WebSocket, Notifier |
| `trade_events` | `trade_closed` | `{trade_id, symbol, reason, exit_price, pnl}` | OrderManager | WebSocket, Notifier |
| `risk_updates` | `risk_alert` | existing RiskMonitor payload | RiskMonitor | WebSocket, Notifier |
| `system_alerts` | `system_alert` | `{alert_type, message, severity, ...details}` | `publish_system_alert()` | WebSocket, Notifier |

Every message is `{"event": ..., "payload": {...}, "timestamp": "<ISO UTC>"}`, built only by `events.envelope()`.
Redis key `price:{SYMBOL}` = `{"price": "<decimal str>", "timestamp": "<ISO UTC>"}`, written only by MarketDataFeed.

### 3.4 Background tasks (all owned and stopped by `TradingEngine`)

| Task | Interval | Gated by | Owner |
|------|----------|----------|-------|
| IBKR connection supervisor | checks every 5 s; backoff 5→60 s | — | IBKRClient |
| Feed universe sync | 60 s | broker connected | MarketDataFeed |
| Monitor DB resync | 5 s | — | PositionMonitor |
| Stale-trade sweep | 1 h | market open | PositionMonitor |
| Price-feed watchdog | 30 s | market open (after a grace period at the open) | PositionMonitor |
| Strategy cycle | 60 s | D3 window, not paused, broker connected | StrategyScheduler |
| Reconciler | 120 s + startup + every reconnect | broker connected; order-placing actions only while market open | Reconciler |
| Risk monitor | 30 s | — | RiskMonitor |
| Bar refresh | checks every 60 s; daily at 16:30 ET + once after startup | trading day, broker connected | BarRefreshJob |
| Daily summary | checks every 60 s; daily at 16:45 ET | trading day | DailySummaryJob |
| Engine lock renew | 10 s | — | EngineLock |
| Notification subscriber | streaming, supervised | — | TradingEngine |

### 3.5 New and changed surfaces

New backend modules: `app/core/engine.py`, `app/core/market_calendar.py`, `app/core/events.py`, `app/core/tasks.py`, `app/core/trading_state.py`, `app/core/engine_lock.py`, `app/core/jobs/bar_refresh.py`, `app/core/jobs/daily_summary.py`, `app/core/execution/reconciler.py`, `app/core/strategy_engine/builtin.py`, `app/core/strategy_engine/seed.py`, `app/db/models/strategy_signal.py`, `app/db/repositories/signal_repo.py`, `app/api/v1/trading.py`, `app/api/v1/signals.py`.

New or changed API endpoints (all under `/api/v1`):

| Method | Path | Task |
|--------|------|------|
| GET | `/strategies/types` | B8 |
| POST | `/strategies` | B8 |
| GET | `/trades/active` | E3 (backend part) |
| POST | `/trades` (manual order) | D3 |
| POST | `/trades/{id}/close` | D2 |
| GET | `/trading/status` | D1 |
| POST | `/trading/pause`, `/trading/resume`, `/trading/flatten` | D1, D2 |
| GET | `/signals?symbol=&strategy_id=&limit=` | E6 (backend part) |
| GET | `/symbols/{ticker}/bars?days=` | E6 (backend part) |
| GET | `/system/health` (adds `engine` component) | D6 |
| GET | `/risk/circuit-breaker` (adds thresholds, trading date, streaks) | C7 |

---

## 4. Tasks

### Phase A — Foundation

#### A1 — Centralize settings; fix env templates  [P0] [S]
- [x] Done
- **Fixes:** F28, F35 (part)
- **Files:** `backend/app/config.py`, `backend/app/dependencies.py`, `backend/app/main.py`, `.env.example`, `env.example` (delete), `.claude/specs/environment-setup.md`
- **Steps:**
  1. Add the fields below to `Settings`. Keep existing fields unchanged.
  2. Use them where today's code hardcodes values: `RiskManager(min_reward_to_risk=..., max_portfolio_risk_pct=...)`, `RiskMonitorConfig(max_aggregate_risk_pct=...)`, `_build_circuit_breaker()` → `CircuitBreaker(daily_loss_limit_pct=..., max_consecutive_losses=...)`, `PositionMonitor(stale_trade_max_days=...)`, `StrategyScheduler(interval_seconds=...)`, CORS `allow_origins=settings.cors_origins`.
  3. Add every new variable to `.env.example` with a one-line comment. Delete `env.example` (stale duplicate).
  4. In `environment-setup.md`: document the new variables; fix ports (IB Gateway: 4002 paper / 4001 live; Docker image: 4004 paper / 4003 live); say that `BROKER` (not `ENVIRONMENT`) selects mock vs IBKR.
- **Pattern:**
  ```python
  from datetime import time
  from decimal import Decimal

  from pydantic import field_validator


  class Settings(BaseSettings):
      # ... existing fields stay exactly as they are ...

      # Trading engine
      engine_enabled: bool = True
      log_dir: str = "logs"
      cors_origins: list[str] = ["http://localhost:5173"]   # env value is JSON: '["https://x"]'
      scheduler_interval_seconds: int = 60
      strategy_eval_start_et: time = time(9, 45)
      strategy_eval_end_et: time = time(15, 45)
      bar_refresh_time_et: time = time(16, 30)
      daily_summary_time_et: time = time(16, 45)
      price_stale_seconds: int = 120

      # Risk — the 1% per-trade rule is deliberately NOT a setting
      max_portfolio_risk_pct: Decimal = Decimal("0.05")
      min_reward_to_risk: Decimal = Decimal("2.0")
      daily_loss_limit_pct: Decimal = Decimal("0.03")
      max_consecutive_losses: int = 5
      stale_trade_max_days: int = 14

      # Execution / broker
      order_fill_timeout_seconds: int = 30
      broker_protective_stops: bool = True
      ibkr_market_data_type: int = 1   # 1 live, 3 delayed (paper only)

      @field_validator("max_portfolio_risk_pct")
      @classmethod
      def _cap_portfolio_risk(cls, value: Decimal) -> Decimal:
          if value <= 0 or value > Decimal("0.10"):
              raise ValueError("max_portfolio_risk_pct must be > 0 and <= 0.10")
          return value
  ```
- **Tests:** `tests/unit/test_config.py`: defaults load; `MAX_PORTFOLIO_RISK_PCT=0.11` raises `ValidationError`; `STRATEGY_EVAL_START_ET=10:00` parses to `time(10, 0)`; `CORS_ORIGINS='["https://a"]'` parses to a list.
- **Done when:** no hardcoded risk/engine numbers remain in constructors' *call sites* (defaults in class signatures may stay for unit tests); `env.example` is gone.

#### A2 — Market calendar  [P0] [S]
- [x] Done
- **Deviation:** pinned `tzdata==2025.2` (not 2026.5): ib_async==2.1.0 requires tzdata>=2025.2,<2026.0 and 2026.5 broke `docker build ./backend`.
- **Fixes:** F17 (foundation)
- **Files:** `backend/requirements.txt`, new `backend/app/core/market_calendar.py`, new `backend/tests/unit/test_market_calendar.py`
- **Steps:**
  1. Add `exchange_calendars==4.13.2` and `tzdata==2026.5` to `requirements.txt` (verified compatible with the pinned pandas 2.2.3 / numpy 2.2.1 / Python 3.11). `tzdata` is required because slim Docker images may not ship a timezone database.
  2. Create the module below. API facts verified on 2026-10-19: `is_session`, `session_open`/`session_close` (return UTC-aware `pd.Timestamp`, early closes handled), `date_to_session(..., direction="previous")`; the default calendar ends about one year after creation, so the calendar is cached per year.
- **Pattern:**
  ```python
  """NYSE calendar helpers. All datetimes are timezone-aware."""

  from __future__ import annotations

  from datetime import date, datetime, time
  from functools import lru_cache
  from zoneinfo import ZoneInfo

  import exchange_calendars as xcals
  import pandas as pd

  ET = ZoneInfo("America/New_York")


  @lru_cache(maxsize=2)
  def _calendar(year: int):
      return xcals.get_calendar("XNYS")


  def _cal():
      return _calendar(datetime.now(ET).year)


  def now_et() -> datetime:
      return datetime.now(ET)


  def trading_date(now: datetime | None = None) -> date:
      return (now or now_et()).astimezone(ET).date()


  def is_trading_day(day: date) -> bool:
      return bool(_cal().is_session(pd.Timestamp(day)))


  def session_bounds(day: date) -> tuple[datetime, datetime] | None:
      if not is_trading_day(day):
          return None
      ts = pd.Timestamp(day)
      return _cal().session_open(ts).to_pydatetime(), _cal().session_close(ts).to_pydatetime()


  def is_market_open(now: datetime | None = None) -> bool:
      now = (now or now_et()).astimezone(ET)
      bounds = session_bounds(now.date())
      return bounds is not None and bounds[0] <= now < bounds[1]


  def in_session_window(start: time, end: time, now: datetime | None = None) -> bool:
      now = (now or now_et()).astimezone(ET)
      bounds = session_bounds(now.date())
      if bounds is None:
          return False
      window_start = max(bounds[0], datetime.combine(now.date(), start, ET))
      window_end = min(bounds[1], datetime.combine(now.date(), end, ET))
      return window_start <= now < window_end


  def previous_trading_day(day: date) -> date:
      prior = pd.Timestamp(day) - pd.Timedelta(days=1)
      return _cal().date_to_session(prior, direction="previous").date()
  ```
- **Tests:** 2026-11-26 (Thanksgiving) is not a trading day; 2026-11-27 closes at 13:00 ET (`session_bounds` close = 18:00 UTC); 2026-10-19 10:00 ET is open, 09:29 ET and 16:00 ET are closed; Saturday is closed; `previous_trading_day(date(2026, 10, 19)) == date(2026, 10, 16)`; `in_session_window(time(9,45), time(15,45), ...)` true at 10:00 ET, false at 09:40 ET and on 2026-11-27 at 13:30 ET.
- **Done when:** tests pass; nothing else imports `exchange_calendars` directly.

#### A3 — Event contract, task helpers, reserved-key fix  [P0] [S]
- [x] Done
- **Fixes:** F26, F15 (foundation), F22 (contract)
- **Files:** new `backend/app/core/events.py`, new `backend/app/core/tasks.py`, `backend/app/notifications/dispatcher.py:209`, `backend/app/brokers/ibkr/client.py:152`, new `backend/tests/unit/test_logging_extra_keys.py`, new `backend/tests/unit/test_tasks.py`
- **Steps:**
  1. Create `events.py` (channels, `envelope()`, `publish_system_alert()`).
  2. Create `tasks.py` (`spawn`, `PeriodicTask`, `supervised_subscribe`).
  3. Rename the two reserved `extra` keys: dispatcher `"message"` → `"alert_message"`; IBKR client `"message"` → `"ib_message"`.
  4. Add the AST guard test below so the bug cannot return.
  5. Do **not** refactor all existing channel constants now; later tasks switch each file to `events.py` as they touch it.
- **Pattern (`events.py`):**
  ```python
  TRADE_EVENTS = "trade_events"
  RISK_UPDATES = "risk_updates"
  SYSTEM_ALERTS = "system_alerts"
  WATCHLIST_PRICES = "watchlist_prices"


  def envelope(event: str, payload: dict) -> str:
      return json.dumps(
          {"event": event, "payload": payload, "timestamp": datetime.now(UTC).isoformat()},
          default=str,
      )


  async def publish_system_alert(cache, alert_type: str, message: str, *, severity: str = "CRITICAL", **details) -> None:
      """Log and publish a system_alert; never raises."""
      log = error_logger.error if severity == "CRITICAL" else system_logger.warning
      log("SYSTEM ALERT", extra={"alert_type": alert_type, "alert_message": message, "severity": severity, **details})
      if cache is None:
          return
      try:
          payload = {"alert_type": alert_type, "message": message, "severity": severity, **details}
          await cache.publish(SYSTEM_ALERTS, envelope("system_alert", payload))
      except Exception as exc:
          system_logger.warning("publish_system_alert failed", extra={"alert_type": alert_type, "error": str(exc)})
  ```
  (`message` is fine inside the JSON *payload*; it is only forbidden as a logging `extra` key.)
- **Pattern (`tasks.py`):**
  ```python
  def spawn(coro: Coroutine[Any, Any, Any], *, name: str, registry: set[asyncio.Task]) -> asyncio.Task:
      """Create a task and keep a strong reference until it finishes."""
      task = asyncio.get_running_loop().create_task(coro, name=name)
      registry.add(task)
      task.add_done_callback(registry.discard)
      return task


  class PeriodicTask:
      """Run an async function every interval; log and survive its exceptions."""

      def __init__(self, name: str, interval_seconds: float, fn: Callable[[], Awaitable[None]], *, initial_delay: float = 0.0) -> None:
          self._name, self._interval, self._fn, self._initial_delay = name, interval_seconds, fn, initial_delay
          self._task: asyncio.Task | None = None

      def start(self) -> None:
          if self._task is None:
              self._task = asyncio.get_running_loop().create_task(self._loop(), name=self._name)

      async def stop(self) -> None:
          if self._task is not None:
              self._task.cancel()
              with suppress(asyncio.CancelledError):
                  await self._task
              self._task = None

      async def _loop(self) -> None:
          if self._initial_delay:
              await asyncio.sleep(self._initial_delay)
          while True:
              try:
                  await self._fn()
              except asyncio.CancelledError:
                  raise
              except Exception:
                  system_logger.exception("Periodic task failed", extra={"task": self._name})
              await asyncio.sleep(self._interval)


  async def supervised_subscribe(cache, channels: list[str], handler: Callable[[dict], Awaitable[None]], *, name: str) -> None:
      """Consume Redis pub/sub forever, reconnecting with backoff on connection errors."""
      backoff = 1.0
      while True:
          try:
              async for msg in cache.subscribe_many(channels=channels, patterns=[]):
                  backoff = 1.0
                  try:
                      await handler(msg)
                  except Exception:
                      system_logger.exception("Subscriber handler failed", extra={"subscriber": name})
          except asyncio.CancelledError:
              raise
          except Exception as exc:
              system_logger.warning("Subscriber lost Redis connection — retrying", extra={"subscriber": name, "error": str(exc), "retry_in_seconds": backoff})
              await asyncio.sleep(backoff)
              backoff = min(backoff * 2, 30.0)
  ```
- **Pattern (`test_logging_extra_keys.py`):**
  ```python
  import ast, logging, pathlib

  RESERVED = set(logging.LogRecord("n", 0, "p", 0, "m", None, None).__dict__) | {"message", "asctime"}


  def test_no_reserved_keys_in_logging_extra():
      hits = []
      for path in pathlib.Path("app").rglob("*.py"):
          for node in ast.walk(ast.parse(path.read_text())):
              if isinstance(node, ast.Call):
                  for kw in node.keywords:
                      if kw.arg == "extra" and isinstance(kw.value, ast.Dict):
                          hits += [f"{path}:{k.lineno} {k.value}" for k in kw.value.keys
                                   if isinstance(k, ast.Constant) and k.value in RESERVED]
      assert not hits, hits
  ```
- **Tests:** the guard test; `PeriodicTask` keeps running after `fn` raises and stops cleanly; `spawn` registry empties after completion; `supervised_subscribe` reconnects when `subscribe_many` raises (fake cache that raises once, then yields one message); `NotificationDispatcher.notify_system_alert` with a real INFO-level logger calls `_send` (patch `_send_sync`).
- **Done when:** the guard test passes and a system alert reaches `_send`.

#### A4 — TradingEngine; hermetic tests  [P0] [M]
- [x] Done
  - Deviation: `TradingEngine` builds its components in `__init__` (not `start()`); the tests/conftest.py unit assertions live in `tests/unit/test_engine_isolation.py`.
- **Fixes:** F20, F30; foundation for every later task
- **Files:** new `backend/app/core/engine.py`, new `backend/app/core/strategy_engine/builtin.py`, `backend/app/main.py`, `backend/app/dependencies.py`, `backend/app/monitoring/logger.py` (no change needed: `setup_logging` already takes `log_dir`), `backend/tests/conftest.py`, new `backend/tests/integration/conftest.py`, every `backend/tests/integration/test_*.py`, `.github/workflows/ci.yml`
- **Steps:**
  1. `builtin.py`: `def load_builtin_strategies() -> None:` containing the six `import app.core.strategy_engine.<x>  # noqa: F401` lines now in `main.py`'s lifespan. Call it at **module level** in `main.py` (so `/strategies/types` works even when the engine is disabled).
  2. `engine.py`: class `TradingEngine(settings)` that builds and owns every component currently created in `main.py`'s lifespan (broker via `_build_broker()`, cache via `_build_cache()` — **remove the second `RedisCache`**, circuit breaker via `_build_circuit_breaker()`, risk manager, order manager, position monitor, scheduler, risk monitor, notification subscriber). Methods: `async start()`, `async stop()`; attributes `started: bool`, plus a `spawn(coro, name)` helper using `tasks.spawn` with `self._tasks`.
  3. `start()` order: connect broker (catch and log non-guard errors) → position monitor → feed (B3) → scheduler → risk monitor → notifier (use `supervised_subscribe`) → periodic jobs added in later tasks.
  4. `stop()` order: scheduler → jobs/reconciler → position monitor (waits for in-flight closes) → feed → broker → notifier → risk monitor → `cache.close()`.
  5. `main.py` lifespan becomes: `setup_logging(log_level=settings.log_level, log_dir=settings.log_dir)`; security audit; if `settings.engine_enabled`: create, start and store the engine in `app.state.engine`; on exit stop it.
  6. `dependencies.py`: add `get_engine(request: Request) -> TradingEngine` that raises `HTTPException(503, "Trading engine is not running")` when missing or not started.
  7. `tests/conftest.py`: before importing `app.main`, set `os.environ["ENGINE_ENABLED"] = "false"` and `os.environ.setdefault("LOG_DIR", tempfile.mkdtemp(prefix="tradingbot-test-logs-"))`.
  8. `tests/integration/conftest.py`: refuse to run unless the database name ends with `_test` (pattern below).
  9. In every integration test module, replace `os.environ.get("DATABASE_URL", "...trading_bot")` and `os.environ.get("REDIS_URL", "...")` with `os.environ["DATABASE_URL"]` / `os.environ["REDIS_URL"]` (no fallback).
  10. `ci.yml`: change the test job's `REDIS_URL` to `redis://localhost:6379/1`.
- **Pattern (`tests/integration/conftest.py`):**
  ```python
  import os

  import pytest
  from sqlalchemy.engine import make_url

  _url = os.environ.get("DATABASE_URL")
  if not _url or not (make_url(_url).database or "").endswith("_test"):
      raise pytest.UsageError(
          "Integration tests TRUNCATE tables. Export DATABASE_URL pointing at a database whose "
          "name ends in _test (see .claude/specs/production-readiness-plan.md §0.2)."
      )
  ```
- **Tests:** existing suites still pass; new unit test asserts that under pytest `get_settings().engine_enabled is False` and `get_settings().log_dir != "logs"`; `TestClient(app)` startup does not touch `backend/logs/` (assert the file `backend/logs/audit.log` mtime is unchanged across a client session — or simply that `settings.log_dir` is the temp dir).
- **Done when:** `main.py` has no component wiring left; running the unit suite leaves `backend/logs/*` untouched; integration tests error out clearly without a `_test` database.

---

### Phase B — Make the live loop work

#### B1 — IBKR tick sanitizing and callback hygiene  [P0] [S]
- [x] Done
  - Deviation: Replaced the obsolete tests/unit/brokers/test_ibkr_reconnect.py (tested the deleted reconnect loop) with test_ibkr_ticks.py; `math.isnan` used instead of `util.isNan`; `disconnect()` now also detaches the pendingTickers handler.
- **Fixes:** F10, F32, F15 (part)
- **Files:** `backend/app/brokers/ibkr/client.py`, `backend/app/brokers/mock/client.py`, `backend/tests/unit/brokers/`
- **Steps:**
  1. Add `_positive_decimal()` (below). Use **only** `ticker.last` as the price; skip the tick if it is missing, NaN, infinite, or ≤ 0. Never fall back to `ticker.close`.
  2. Compute `day_change` / `day_change_pct` from `ticker.close` (previous session close) when it is valid.
  3. Emit only when the price changed or 5 s passed since the last emit for that symbol (keeps the price key fresh without flooding Redis).
  4. Dispatch callbacks with `spawn(..., registry=self._tasks)`.
  5. In `subscribe_price_feed` (IBKR and Mock): append the callback only if it is not already registered.
- **Pattern:**
  ```python
  def _positive_decimal(value: float | None) -> Decimal | None:
      if value is None or util.isNan(value) or math.isinf(value) or value <= 0:
          return None
      return Decimal(str(value))


  def _on_pending_tickers(self, tickers: set) -> None:
      now = datetime.now(UTC)
      for ticker in tickers:
          price = _positive_decimal(ticker.last)
          if price is None:
              continue
          symbol = ticker.contract.symbol
          last = self._last_emitted.get(symbol)
          if last and last[0] == price and (now - last[1]).total_seconds() < 5:
              continue
          self._last_emitted[symbol] = (price, now)
          prev_close = _positive_decimal(ticker.close)
          update = PriceUpdate(
              ticker=symbol, price=price,
              bid=_positive_decimal(ticker.bid), ask=_positive_decimal(ticker.ask),
              day_change=(price - prev_close) if prev_close else None,
              day_change_pct=((price - prev_close) / prev_close) if prev_close else None,
              timestamp=now,
          )
          for cb in self._price_callbacks:
              spawn(cb(update), name=f"tick-{symbol}", registry=self._tasks)
  ```
- **Tests:** fake tickers (`SimpleNamespace` with `contract=SimpleNamespace(symbol="AAPL")`, `last=float("nan")`, …): NaN, `-1`, `0`, and `None` produce no callback; a valid tick produces exactly one callback even after `subscribe_price_feed` was called three times; an unchanged price within 5 s is suppressed, and re-emitted after 5 s.
- **Done when:** no NaN can reach `PriceUpdate`.

#### B2 — IBKR connection supervisor and account guard  [P0] [M]
- [x] Done
  - Deviation: Old test_ibkr_reconnect.py deleted, replaced by test_ibkr_supervisor.py; the engine guard handling (step 9) is in `TradingEngine.start()`; `connect()` resets `_stopping` so a client can be reconnected after `disconnect()`.
- **Fixes:** F9, F24, hardening item 7
- **Files:** `backend/app/brokers/ibkr/client.py`, `backend/tests/unit/brokers/`
- **Steps:**
  1. Add `class LiveTradingGuardError(RuntimeError)`; `_enforce_live_trading_guard` raises it.
  2. `connect()`: run the env guard, try one `_connect_once()`; on a *connectivity* error just log a warning; re-raise `LiveTradingGuardError`; then start the supervisor task (once).
  3. `_connect_once()`: `connectAsync(host, port, clientId=..., timeout=10)` → `_verify_account_mode()` → `reqMarketDataType(settings.ibkr_market_data_type)` → re-request market data for every contract in `_subscribed_contracts`.
  4. `_verify_account_mode()`: paper account IDs start with `"D"` (e.g. `DU…`). If mode is paper and any managed account does not start with `"D"`: disconnect and raise `LiveTradingGuardError`. Log only two-letter prefixes, never full account IDs.
  5. `_supervise()` (pattern below) replaces `_reconnect_loop` and `_RECONNECT_DELAYS`. Delete both. `_on_disconnected` now only logs.
  6. `add_reconnect_listener(fn)`; the supervisor awaits each listener after a successful reconnect (the engine registers the reconciler in C5).
  7. `disconnect()`: set `_stopping`, cancel and await the supervisor, then disconnect.
  8. Make backoff, alert threshold and check interval constructor keyword arguments (defaults below) so tests run fast.
  9. Engine (`A4`): if `connect()` raises `LiveTradingGuardError`, log CRITICAL, publish `BROKER_ACCOUNT_MODE_MISMATCH`, and do not start trading components.
- **Pattern:**
  ```python
  async def _supervise(self) -> None:
      loop = asyncio.get_running_loop()
      down_since: float | None = None
      attempt = 0
      alerted = False
      while not self._stopping:
          if self._ib.isConnected():
              if alerted:
                  await publish_system_alert(self._cache, "BROKER_RECONNECTED", "IB Gateway connection restored", severity="INFO")
              down_since, attempt, alerted = None, 0, False
              await asyncio.sleep(self._check_seconds)
              continue
          down_since = down_since or loop.time()
          await asyncio.sleep(self._backoff[min(attempt, len(self._backoff) - 1)])
          attempt += 1
          try:
              await self._connect_once()
          except LiveTradingGuardError as exc:
              await publish_system_alert(self._cache, "BROKER_ACCOUNT_MODE_MISMATCH", str(exc))
              self._stopping = True
              return
          except Exception as exc:
              system_logger.warning("IBKRClient: reconnect attempt failed", extra={"attempt": attempt, "error": str(exc)})
              if not alerted and loop.time() - down_since >= self._alert_after:
                  await publish_system_alert(
                      self._cache, "BROKER_DISCONNECTED",
                      f"IB Gateway unreachable for {int(loop.time() - down_since)}s; still retrying",
                      host=self._settings.ibkr_host, port=self._settings.ibkr_port,
                  )
                  alerted = True
              continue
          for listener in self._reconnect_listeners:
              try:
                  await listener()
              except Exception:
                  system_logger.exception("IBKRClient: reconnect listener failed")
  ```
  Defaults: `backoff_seconds=(5, 10, 20, 40, 60)` (stays at 60 forever), `alert_after_seconds=300`, `check_seconds=5`.
- **Tests (fake `IB` object assigned to `client._ib`):** initial connect fails, then succeeds on the 2nd supervisor attempt, and `reqMktData` is called once per previously subscribed contract; paper mode + account `"U1234567"` raises `LiveTradingGuardError` and calls `disconnect`; an alert is published once after the threshold (threshold 0.05 s, backoff 0.01 s); `disconnect()` stops the supervisor.
- **Done when:** the bot reconnects after any outage length without a restart and re-subscribes prices.

#### B3 — Wire MarketDataFeed into the engine  [P0] [M]
- [x] Done
  - Deviation: `WATCHLIST_PRICES_CHANNEL` kept in feed.py as an alias of `events.WATCHLIST_PRICES`; engine does an initial `feed.sync()` after the position monitor starts, then the 60 s PeriodicTask; `_kick_feed_sync` helper in symbols.py.
- **Fixes:** F1, F2 (with B4), F22 (backend contract)
- **Files:** `backend/app/data/feed.py`, `backend/app/core/engine.py`, `backend/app/api/websocket.py`, `backend/app/api/v1/symbols.py`, `backend/tests/unit/data/test_feed.py`
- **Steps:**
  1. Constructor: `MarketDataFeed(broker, cache, session_factory)`. Add `add_listener(fn)` and `last_tick_at(symbol) -> datetime | None`.
  2. Replace `start(session)` with `desired_symbols()` + `sync()` (pattern). Desired = active watchlist ∪ symbols of **enabled** strategies ∪ symbols with an active trade. (Positions must keep their price feed even if removed from the watchlist.)
  3. `_on_price_update`: record last-tick time, call in-process listeners **first** (each wrapped in try/except), then write the Redis key and publish the envelope (§3.3).
  4. Engine: create the feed, `feed.add_listener(position_monitor.on_price_update)` (B4), `PeriodicTask("feed_sync", 60, feed.sync)`.
  5. `websocket.py`: add `"system_alerts"` to `_DASHBOARD_CHANNELS`.
  6. `symbols.py`: after a successful add or delete, if the engine is running, `engine.spawn(engine.feed.sync(), "feed-sync")` (best effort; the periodic sync covers failures).
- **Pattern:**
  ```python
  async def desired_symbols(self) -> set[str]:
      async with self._session_factory() as session:
          watchlist = await SymbolRepo(session).get_all(active_only=True)
          strategies = await StrategyRepo(session).get_enabled()
          active = await TradeRepo(session).get_active_trades()
      symbols = {s.ticker.upper() for s in watchlist}
      for strategy in strategies:
          symbols.update(str(sym).upper() for sym in strategy.config.get("symbols", []))
      symbols.update(t.symbol.upper() for t in active)
      return symbols

  async def sync(self) -> None:
      if not self._broker.is_connected():
          return
      desired = await self.desired_symbols()
      for ticker in sorted(desired - self._subscribed):
          await self.add_symbol(ticker)
      for ticker in sorted(self._subscribed - desired):
          await self.remove_symbol(ticker)

  async def _on_price_update(self, update: PriceUpdate) -> None:
      symbol = update.ticker.upper()
      self._last_tick_at[symbol] = update.timestamp
      for listener in self._listeners:
          try:
              await listener(update)
          except Exception:
              system_logger.exception("MarketDataFeed: listener failed", extra={"ticker": symbol})
      try:
          await self._cache.set(f"price:{symbol}", json.dumps({"price": str(update.price), "timestamp": update.timestamp.isoformat()}))
          change = str(update.day_change_pct) if update.day_change_pct is not None else None
          await self._cache.publish(WATCHLIST_PRICES, envelope("price_update", {"symbol": symbol, "price": str(update.price), "change_pct": change}))
      except Exception as exc:
          system_logger.error("MarketDataFeed: failed to forward price update", extra={"ticker": symbol, "error": str(exc)})
  ```
- **Tests:** desired set is the union (fake repos); `sync()` subscribes new and unsubscribes removed tickers; a tick calls the listener before any Redis write (record call order in a fake); Redis key JSON has `price` and `timestamp`; the published message is a `price_update` envelope with `symbol`.
- **Done when:** with MockBroker, `simulate_price_tick` results in the key, the published envelope, and the listener call.

#### B4 — PositionMonitor: in-process prices, DB resync, market-hours gate, watchdog  [P0] [M]
- [x] Done
  - Deviation: Watchdog also stays quiet for `price_stale_seconds` after the monitor starts (not only after the session open); extra constructor kwargs `clock` and `watchdog_interval_seconds`; `test_layer5_checkpoint.py` updated for the new feed constructor/envelope.
- **Fixes:** F2, F15, F17 (exits), hardening items 3 and 4
- **Files:** `backend/app/core/execution/position_monitor.py`, `backend/app/core/engine.py`, `backend/tests/unit/execution/test_position_monitor.py`, `backend/tests/integration/test_layer17_e2e.py` (`TestPositionMonitorIntegration`)
- **Steps:**
  1. Remove the Redis subscription loop (`_run`) and `_handle_trade_event`. The monitor no longer depends on Redis for anything safety-critical.
  2. Add `async def on_price_update(self, update: PriceUpdate)`: return immediately unless `self._is_market_open()`; then run today's body of `_handle_price` from "Check all tracked trades for this ticker" onward, using `update.price`.
  3. Add `async def _resync()` (pattern): merge DB OPEN trades into `_tracked` (keep trailing state of trades already tracked; skip ids in `_closing`; drop ids no longer OPEN). Run it in `start()` (log a warning if the DB is down) and every 5 s via `PeriodicTask`.
  4. Make `_sweep_stale_trades` async, gated by market open, closing with reason `"STALE_TIMEOUT"`; run it via `PeriodicTask` (interval 3600 s, initial delay 3600 s).
  5. All closes go through `spawn(self._close(...), registry=self._tasks)`. `stop()`: stop periodic tasks, then `await asyncio.wait(self._tasks, timeout=20)` so in-flight closes finish.
  6. Price-feed watchdog every 30 s while the market is open (skip the first `price_stale_seconds` after the session open): for each tracked symbol whose last tick is missing or older than `price_stale_seconds`, publish `PRICE_FEED_STALE` once per symbol per day. Take `last_tick_at` as a constructor callable (the feed's method).
  7. Constructor gains: `cache`, `is_market_open`, `last_tick_at`, `resync_interval_seconds=5.0`, `price_stale_seconds`.
  8. Rewrite the integration tests in `TestPositionMonitorIntegration` to drive prices through `MarketDataFeed` + `MockBroker.simulate_price_tick` instead of publishing to `price:TSLA`.
- **Pattern:**
  ```python
  async def _resync(self) -> None:
      async with self._session_factory() as session:
          open_trades = await TradeRepo(session).get_open_trades()
      open_ids = {t.id for t in open_trades}
      for trade in open_trades:
          if trade.id not in self._tracked and trade.id not in self._closing:
              self._track(trade)
      for trade_id in list(self._tracked):
          if trade_id not in open_ids:
              self._tracked.pop(trade_id, None)
  ```
- **Tests:** price below stop with market open → `close_position(…, "STOP_LOSS")` called once; with `is_market_open=lambda: False` → never called; resync adds a new DB trade and removes a closed one while preserving a ratcheted trailing stop on a surviving trade; `stop()` waits for an in-flight close; watchdog alerts once for a stale symbol and not during the open grace period.
- **Done when:** the monitor works with Redis completely down.

#### B5 — Publish `trade_executed` on every fill  [P0] [S]
- [x] Done
- **Fixes:** F3
- **Files:** `backend/app/core/execution/order_manager.py`, `backend/tests/integration/test_layer17_e2e.py`
- **Steps:** right after the OPEN transition in `submit_order`, if `self._cache` and `self._session_factory` are set, open a session and `await TradeHandler(self._cache).on_fill(request, validation, order_result, session)`. Wrap in try/except → `trading_logger.warning` (a publish failure must not fail the order). Remove the "then call TradeHandler.on_fill()" instructions from the module and class docstrings.
- **Tests:** integration: subscribe to `trade_events`, submit an order, assert a `trade_executed` message arrives with the trade id.
- **Done when:** every OPEN transition publishes exactly one `trade_executed`.

#### B6 — Daily bar refresh job  [P0] [M]
- [x] Done
  - Deviation: `BarRefreshJob` also takes an optional `registry=` kwarg (defaults to the global registry); the 2-second pause is not exposed on the engine (tests inject a fake clock and call the job directly).
- **Fixes:** F5
- **Files:** new `backend/app/core/jobs/__init__.py`, new `backend/app/core/jobs/bar_refresh.py`, `backend/app/core/engine.py`, `backend/app/api/v1/symbols.py`, tests
- **Steps:**
  1. `BarRefreshJob(broker, session_factory, cache, *, run_at, clock=now_et, pause_seconds=2.0)` with `tick()`, `desired_symbols()`, `refresh(symbols, duration)` (pattern).
  2. Desired symbols = active watchlist ∪ enabled strategies' `symbols` ∪ every enabled strategy's `required_extra_symbols()` (build each via `registry.build`, skip on error) ∪ active-trade symbols.
  3. Engine: store the job as `engine.bar_refresh` and schedule `PeriodicTask("bar_refresh", 60, engine.bar_refresh.tick)`.
  4. `POST /symbols`: after a successful add, if the engine runs, `engine.spawn(engine.bar_refresh.refresh({ticker}, "2 Y"), "bar-backfill")`.
  5. A symbol counts as failed if the fetch raises **or** returns 0 bars; publish one `DATA_REFRESH_FAILED` alert listing all failed symbols.
- **Pattern:**
  ```python
  async def tick(self) -> None:
      if not self._broker.is_connected():
          return
      if not self._startup_done:
          await self.refresh(await self.desired_symbols(), duration="2 Y")
          self._startup_done = True
          return
      now = self._clock()
      today = now.date()
      if self._last_run == today or not market_calendar.is_trading_day(today) or now.time() < self._run_at:
          return
      await self.refresh(await self.desired_symbols(), duration="10 D")
      self._last_run = today
  ```
  The 2-second pause between symbols respects IB historical-data pacing; never request the same symbol twice within 15 s.
- **Tests:** with MockBroker and an injected clock: before 16:30 ET → no fetch; at 16:31 on a trading day → one refresh, and a second `tick()` the same day does nothing; on a holiday → nothing; the startup backfill runs once; an empty broker response triggers one alert.
- **Done when:** bars for every evaluated symbol (including `SPY` for bull/bear) are at most one trading day old after 16:30 ET.

#### B7 — Scheduler: trading window, completed bars, fresh prices, one action per signal  [P0] [L]
- [x] Done
  - Deviation: `TradeRepo.get_active_trades()` now also includes CLOSING (affects aggregate risk, feed and refresh symbol sets; a closing trade still holds exposure). `OrderManager._compute_aggregate_risk` was replaced by `_load_active_trades`. Scheduler also takes `signal_repo_factory`/`trade_repo_factory` for test fakes; the existing portfolio-cap concurrency integration test now uses two different symbols.
- **Fixes:** F4, F17 (entries), F18, F34 (storage)
- **Files:** new migration `backend/alembic/versions/0007_strategy_signals.py`, new `backend/app/db/models/strategy_signal.py` (register it in `app/db/models/__init__.py`), new `backend/app/db/repositories/signal_repo.py`, `backend/app/db/repositories/trade_repo.py`, `backend/app/core/strategy_engine/scheduler.py`, `backend/app/core/execution/order_manager.py`, tests
- **Steps:**
  1. Migration 0007: table `strategy_signals` (pattern). `gen_random_uuid()` is built into PG15.
  2. `SignalRepo.claim(...) -> uuid.UUID | None` (insert … on conflict do nothing … returning id), `set_outcome(signal_id, outcome, detail=None, trade_id=None)`, `list(symbol=None, strategy_id=None, limit=100)`.
  3. `TradeRepo.has_active_trade(symbol) -> bool` (status in PENDING/SUBMITTED/OPEN/CLOSING).
  4. Scheduler `_run_cycle`: return early unless `market_calendar.in_session_window(start, end, clock())`; return if `is_paused` (an optional async callable, wired in D1); return if the broker is disconnected (log once per day); get the balance (return on error or ≤ 0); set `self.last_cycle_at`.
  5. Bars come **only** from the DB, **only completed sessions**: filter the `OHLCVBar` rows with `row.time.date() < trading_day` *before* converting them to `PriceBar` (after conversion the field is `.timestamp`). Cache the converted list in memory per `(symbol, trading_day)`. Skip with a once-per-day warning if `bars[-1].timestamp.date()` is older than `previous_trading_day(trading_day)`.
  6. Current price comes **only** from the Redis key, and only if its `timestamp` is within `price_stale_seconds`. Keep the consecutive-miss escalation. Delete both broker fallbacks (`_get_current_price`'s historical request, `_get_historical_bars`'s broker fetch).
  7. Set `MarketData.open_position = await has_active_trade(symbol)`. If it is true, skip after `generate_signal` without claiming.
  8. For a BUY/SELL signal: `claim()` with `bar_date = bars[-1].timestamp.date()`; if `None`, return (already acted on this bar). Then size, submit, and record the outcome: `SUBMITTED` (with `trade_id`), `RISK_REJECTED` (detail = `exc.reason`), `ORDER_FAILED` (detail = `str(exc)`), or `ZERO_QUANTITY`.
  9. `OrderManager.submit_order`: inside `_submission_lock`, load active trades once; if any has the same symbol, log to `risk_logger` and raise `RiskRejectionError(reason="SYMBOL_ALREADY_ACTIVE: ...", request=request)`; compute aggregate risk from the same list.
- **Pattern (migration 0007):**
  ```python
  op.create_table(
      "strategy_signals",
      sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
      sa.Column("strategy_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("trading_strategies.id", ondelete="CASCADE"), nullable=False),
      sa.Column("symbol", sa.String(10), nullable=False),
      sa.Column("bar_date", sa.Date(), nullable=False),
      sa.Column("action", sa.String(4), nullable=False),
      sa.Column("entry_price", sa.Numeric(18, 8)),
      sa.Column("stop_loss_price", sa.Numeric(18, 8)),
      sa.Column("take_profit_price", sa.Numeric(18, 8)),
      sa.Column("outcome", sa.String(32), nullable=False, server_default="CLAIMED"),
      sa.Column("detail", sa.Text()),
      sa.Column("trade_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("trades.id", ondelete="SET NULL")),
      sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
      sa.UniqueConstraint("strategy_id", "symbol", "bar_date", "action", name="uq_strategy_signals_dedupe"),
  )
  op.create_index("ix_strategy_signals_symbol_created_at", "strategy_signals", ["symbol", "created_at"])
  ```
- **Pattern (claim):**
  ```python
  stmt = (
      insert(StrategySignal)
      .values(strategy_id=strategy_id, symbol=symbol.upper(), bar_date=bar_date, action=action,
              entry_price=entry_price, stop_loss_price=stop_loss_price, take_profit_price=take_profit_price)
      .on_conflict_do_nothing(constraint="uq_strategy_signals_dedupe")
      .returning(StrategySignal.id)
  )
  return (await self.session.execute(stmt)).scalar_one_or_none()
  ```
  (`insert` is `sqlalchemy.dialects.postgresql.insert`, as in `ohlcv_repo.py`.)
- **Tests (unit, with fakes and an injected clock):** outside the window no strategy is loaded; a strategy that always returns BUY submits once across three cycles (fake `SignalRepo` that returns the id once, then `None`); an active trade → no submit; stale bars → no submit; a stale price key → no submit; bars dated today are excluded. **Integration:** a second `claim()` with the same key returns `None`; OrderManager rejects a second trade for the same symbol with `SYMBOL_ALREADY_ACTIVE`.
- **Done when:** one signal → at most one order, and no IBKR historical requests come from the scheduler.

#### B8 — Strategies: seed, create, validate  [P0] [M]
- [x] Done
  - Deviation: `GET /strategies/types` and validation call `load_builtin_strategies()` so they work without the engine; validation helpers (`config_error`, `normalize_config`, `default_config`) live in `strategy_engine/seed.py` and `config_error` takes the registry as its first argument.
- **Fixes:** F6, F29 (backend)
- **Files:** `backend/app/core/strategy_engine/base.py` and every strategy module, new `backend/app/core/strategy_engine/seed.py`, `backend/app/api/v1/strategies.py`, `backend/app/api/v1/schemas.py`, `backend/app/core/engine.py`, tests
- **Steps:**
  1. Change `get_config_schema` to `@classmethod def get_config_schema(cls) -> dict` in `BaseStrategy` (abstract) and all six strategies (verified: none uses instance state). Instance calls keep working.
  2. `seed.py`: for each registered type except `"composite"` that has no row yet, create one **disabled** row named after the type with default config from the schema; validate with `registry.build` first and skip on error. The engine calls it during `start()`.
  3. `GET /strategies/types` → `[{type, config_schema}]` for every registered type.
  4. `POST /strategies` (`StrategyCreateIn`: `name` 1–255 chars, `type`, `config: dict = {}`, `is_enabled = False`) → 422 `INVALID_STRATEGY_CONFIG` with the exception text if `registry.build(type, config)` fails or the type is unknown; 201 on success.
  5. `PATCH /strategies/{id}`: when `config` is present, validate it the same way against the row's type before saving.
  6. Validate `config.symbols` is a list of 1–10-character strings; uppercase them before saving.
- **Pattern:**
  ```python
  def default_config(schema: dict) -> dict:
      return {key: spec["default"] for key, spec in schema.get("properties", {}).items() if "default" in spec}


  def config_error(type_name: str, config: dict) -> str | None:
      if not registry.is_registered(type_name):
          return f"Unknown strategy type {type_name!r}"
      try:
          registry.build(type_name, config)
      except Exception as exc:
          return str(exc)
      return None
  ```
- **Tests:** seeding twice creates each type once and all disabled; `/strategies/types` lists all six types with schemas; POST with `fast_period >= slow_period` → 422; PATCH with an invalid config → 422 and the row is unchanged.
- **Done when:** a fresh database shows five disabled strategies in the UI that can be configured and enabled.

#### B9 — Engine wiring test and a more realistic MockBroker  [P0] [M]
- [x] Done
  - Deviation: engine intervals are constructor kwargs (`clock`, `is_market_open`, `feed_sync_interval`, `bar_refresh_interval`, `monitor_resync_interval`). The test clears the `_build_*` lru_caches and disposes the DB engine per test (loop-per-test), and adds TSLA to the watchlist so the feed does not drop its price key after the close.
- **Fixes:** the root cause behind F1–F3 (each part tested against an assumed contract, never end to end)
- **Files:** `backend/app/brokers/mock/client.py`, new `backend/tests/integration/test_engine_wiring.py`
- **Steps:**
  1. MockBroker: remember the last simulated price per symbol; fill market orders at it (fallback `100.00`); keep `positions` per symbol updated on fills (BUY adds, SELL subtracts) and return them from `get_positions()`; store orders by `order_ref` (used from C2 on).
  2. Make engine intervals injectable (constructor keyword arguments or settings) so the test can use ~0.1 s periods.
  3. Test 1 — exits: build a `TradingEngine` with `BROKER=mock`, the test DB/Redis, `is_market_open=lambda: True`; `await broker.simulate_price_tick("TSLA", Decimal("200"))` so the mock fills at 200; open a trade through `order_manager.submit_order` (entry 200, stop 195); `await broker.simulate_price_tick("TSLA", Decimal("194"))`; poll until the trade is CLOSED with `exit_reason=STOP_LOSS`; assert `price:TSLA` exists and a `price_update` envelope was published.
  4. Test 2 — entries: register a throwaway strategy class `"test_always_buy"` in the test (unregister in teardown), seed bars for yesterday and earlier, enable it for one symbol, set a fresh price key, run three scheduler cycles inside a forced window → exactly one trade and one `strategy_signals` row.
- **Done when:** both tests pass in CI. Any future change that disconnects the loop fails here.

---

### Phase C — Execution safety

#### C1 — Trade execution columns  [P0] [S]
- [x] Done
- **Deviation:** also added `set_exit_order()` and `set_stop_order(trade_id, order_id, stop_price=None)` to TradeRepo for C4/C6.
- **Fixes:** F13 (data), groundwork for C2–C6
- **Files:** new migration `0008_trade_execution_fields.py`, `backend/app/db/models/trade.py`, `backend/app/db/repositories/trade_repo.py`, `backend/app/api/v1/schemas.py`
- **Steps:**
  1. Add nullable columns to `trades`: `entry_order_id VARCHAR(32)`, `entry_fill_price NUMERIC(18,8)`, `filled_quantity NUMERIC(18,8)`, `exit_order_id VARCHAR(32)`, `stop_order_id VARCHAR(32)`, `current_stop_price NUMERIC(18,8)`; plus `close_attempts INTEGER NOT NULL DEFAULT 0`.
  2. Add enum values `STALE_TIMEOUT` and `EXTERNAL` to `exitreason` (inside `autocommit_block()`, `ADD VALUE IF NOT EXISTS`); add them to `ExitReason`.
  3. `TradeRepo`: `record_entry_fill(trade_id, order_id, fill_price, filled_qty)`, `set_entry_order(trade_id, order_id)`, `set_stop_order(trade_id, order_id)`, `begin_close(trade_id, exit_reason, *, allow_from_closing=False) -> Trade | None` (atomic, pattern), `finish_close(trade_id, exit_price, pnl, exit_order_id) -> Trade | None` (atomic `WHERE status = 'CLOSING'`), `has_active_trade(symbol)` (if not added in B7).
  4. `TradeOut` gains: `take_profit_price`, `reward_to_risk_ratio`, `exit_reason`, `entry_fill_price`, `filled_quantity`, `current_stop_price`, `close_attempts`.
- **Pattern:**
  ```python
  async def begin_close(self, trade_id, exit_reason: ExitReason, *, allow_from_closing: bool = False) -> Trade | None:
      allowed = (TradeStatus.OPEN, TradeStatus.CLOSING) if allow_from_closing else (TradeStatus.OPEN,)
      stmt = (
          update(Trade)
          .where(Trade.id == trade_id, Trade.status.in_(allowed))
          .values(status=TradeStatus.CLOSING, exit_reason=exit_reason, close_attempts=Trade.close_attempts + 1)
          .returning(Trade)
      )
      return (await self.session.execute(stmt)).scalar_one_or_none()
  ```
- **Tests:** integration: two concurrent `begin_close` calls on the same OPEN trade → exactly one returns the trade; `finish_close` on a CLOSED trade returns `None`; the migration downgrades cleanly (`alembic downgrade -1 && alembic upgrade head`). Note: Postgres cannot drop enum values; the downgrade drops only the columns.
- **Done when:** fills and order ids are persisted, separate from the audit-immutable columns.

#### C2 — Order lifecycle: WORKING status, order refs, cancel on timeout  [P0] [M]
- [x] Done
- **Deviation:** MockBroker STP orders rest as WORKING by default (for C6); an exception from every retry still maps to ERROR and CANCELLED (reconciler C5 must alert on untracked broker positions).
- **Fixes:** F7
- **Files:** `backend/app/brokers/base.py`, `backend/app/brokers/ibkr/client.py`, `backend/app/brokers/ibkr/mapper.py`, `backend/app/brokers/mock/client.py`, `backend/app/core/execution/order_manager.py`, tests
- **Steps:**
  1. `OrderRequest`: `order_type: Literal["MKT", "LMT", "STP"]`, new `stop_price`, `tif: Literal["DAY", "GTC"] = "DAY"`, `order_ref: str = ""`. **Remove** the unused `stop_loss_price` field (the broker never used it; C6 replaces it). `OrderResult`: add `"WORKING"` to `status` and `order_ref: str = ""`.
  2. `mapper.map_order_result` (pattern): `Filled` → FILLED; `Cancelled`/`ApiCancelled`/`Inactive` → PARTIAL if anything filled, else REJECTED; `PendingSubmit`/`PreSubmitted`/`Submitted`/`ApiPending`/`PendingCancel` → PARTIAL if anything filled, else WORKING; anything else → ERROR. Delete `_STATUS_MAP`.
  3. `IBKRClient.place_order`: build the order with `orderRef` and `tif`; for MKT/LMT wait up to `order_fill_timeout_seconds` for `isDone()`; if not done, `cancelOrder` and wait up to 10 s more; then map. For STP, wait only until acknowledged (`PreSubmitted`/`Submitted`) or done, up to 10 s.
  4. `OrderManager.submit_order` uses `order_ref=f"{trade_id}:entry"` and maps outcomes: FILLED or PARTIAL with `filled_quantity > 0` → `record_entry_fill` → OPEN → `trade_executed` (B5) → protective stop (C6); WORKING → `set_entry_order`, stays **SUBMITTED**, publish `ORDER_STUCK_WORKING` (the reconciler resolves it); REJECTED/ERROR with nothing filled → CANCELLED.
  5. MockBroker: honor `order_ref`; add test toggles `next_status` / `next_filled_quantity` to simulate WORKING, PARTIAL, and REJECTED.
- **Pattern:**
  ```python
  _WORKING = {"PendingSubmit", "PreSubmitted", "Submitted", "ApiPending", "PendingCancel"}
  _CANCELLED = {"Cancelled", "ApiCancelled", "Inactive"}

  status = trade.orderStatus.status
  filled = _to_decimal(str(trade.orderStatus.filled or 0))
  if status == "Filled":
      mapped = "FILLED"
  elif status in _CANCELLED:
      mapped = "PARTIAL" if filled > 0 else "REJECTED"
  elif status in _WORKING:
      mapped = "PARTIAL" if filled > 0 else "WORKING"
  else:
      mapped = "ERROR"
  ```
- **Tests:** mapper cases for every status above; `submit_order` with a WORKING result leaves the trade SUBMITTED with `entry_order_id` set; PARTIAL with 4 of 10 filled → OPEN with `filled_quantity = 4` and `entry_fill_price` set; `place_order` cancels a still-working order after the timeout (fake IB trade object).
- **Done when:** nothing is marked OPEN or CLOSED without a real fill.

#### C3 — Idempotent order retries  [P0] [S]
- [x] Done
- **Deviation:** IBKR `find_order_by_ref` also falls back to `get_execution_summary` when no session trade/open order matches; MockBroker has one-shot `raise_after_next_order` toggle for the retry test.
- **Fixes:** F8
- **Files:** `backend/app/brokers/base.py`, `backend/app/brokers/ibkr/client.py`, `backend/app/brokers/ibkr/mapper.py`, `backend/app/brokers/mock/client.py`, `backend/app/core/execution/order_manager.py`, tests
- **Steps:**
  1. `BaseBroker` gets two abstract methods:
     - `find_order_by_ref(order_ref) -> OrderResult | None`: state of the **latest** order with that ref (for "did my order reach the broker?").
     - `get_execution_summary(order_ref) -> OrderResult | None`: **all** fills with that ref aggregated (total shares, volume-weighted price, status FILLED), for finalizing exits that took several attempts. IBKR executions cover the current trading day only.
  2. IBKR: search `self._ib.trades()` by `t.order.orderRef`; if none, `await self._ib.reqAllOpenOrdersAsync()`; for executions, `await self._ib.reqExecutionsAsync()` and filter on `fill.execution.orderRef`. The trade id is `UUID(order_ref.split(":")[0])`.
  3. MockBroker: keep a dict of orders by ref and a list of fills.
  4. `_place_order_with_retry`: after an exception and before resubmitting, call `find_order_by_ref(broker_request.order_ref)`; if it returns a result, return that result instead of placing another order.
- **Tests:** a fake broker that records the order and then raises → the retry finds it → exactly one order exists; two exit fills under one ref aggregate to the right quantity and average price.
- **Done when:** a retry can never create a second order for the same ref.

#### C4 — Close path hardening  [P0] [M]
- [x] Done
- **Deviation:** `OrderManager` takes injectable `is_market_open` (engine passes its own; integration test helpers pass `lambda: True`). `close_position` retries sell only `filled_quantity` minus shares already sold under `:exit` (via execution summary). If the protective-stop cancel or broker lookup raises, it publishes CLOSE_FAILED and returns FAILED without selling. `finish_close` got an optional `exit_reason`. Circuit-breaker event handling is left to C7. Old test expecting a CRITICAL log on re-close was replaced by a NOT_OPEN assertion.
- **Fixes:** F12, F13, F7 (closes)
- **Files:** `backend/app/core/execution/order_manager.py`, `backend/app/core/execution/position_monitor.py` (reason strings), tests
- **Steps:** rewrite `close_position(trade_id, reason, *, allow_from_closing=False) -> CloseOutcome` as below. Add `class CloseOutcome(str, Enum)`: `CLOSED`, `PENDING_EXIT`, `FAILED`, `NOT_OPEN`, `MARKET_CLOSED`.
  1. If the market is closed → return `MARKET_CLOSED` without touching state.
  2. `begin_close` (atomic; stores the intended `exit_reason`, increments `close_attempts`). `None` → return `NOT_OPEN`.
  3. If `stop_order_id` is set (C6): cancel the protective stop; if it turns out FILLED, finalize as `STOP_LOSS` using its execution summary and return `CLOSED` (no market sell).
  4. Check `find_order_by_ref(f"{id}:exit")`: FILLED → finalize; WORKING → return `PENDING_EXIT`.
  5. Place a market SELL for `filled_quantity` (fallback `quantity` only for legacy rows), `order_ref=f"{id}:exit"`, through the idempotent retry (C3).
  6. FILLED → finalize. WORKING → save `exit_order_id`, return `PENDING_EXIT`. PARTIAL → publish `CLOSE_PARTIAL`, return `FAILED` (stays CLOSING; the reconciler retries the remaining broker quantity). REJECTED/ERROR → publish `CLOSE_FAILED`, return `FAILED` (stays CLOSING; the reconciler retries).
  7. **Finalize** = `get_execution_summary(f"{id}:exit")` (or `:stop`) → `pnl = (exit_avg_price − (entry_fill_price or entry_price)) × exit_shares` → `finish_close` (atomic) → **only if it succeeded**, `circuit_breaker.record_closed_trade(...)` and handle its events (C7) → publish `trade_closed` → metrics snapshot.
  8. Expose the finalize step as a public method for the reconciler (C5): `finalize_close(trade_id, exit_reason, fill: OrderResult | None, fallback_price: Decimal) -> bool`. It never places an order and ignores market hours. If the trade is still OPEN it first calls `begin_close`; it uses `fill.avg_fill_price` × `fill.filled_quantity` when a fill is given, otherwise `fallback_price` × `filled_quantity`; then it runs step 7 from `finish_close` onward.
  9. PositionMonitor passes `"STALE_TIMEOUT"` for stale closes; map reason strings to `ExitReason` with no silent fallback.
- **Tests (integration):** `asyncio.gather` of two `close_position` calls on one trade → exactly one exit order and one CLOSED row; closing a partially filled trade sells `filled_quantity`; PnL uses `entry_fill_price`; a WORKING exit leaves the trade CLOSING with `exit_order_id`; a market-closed call changes nothing; the breaker records PnL exactly once.
- **Done when:** no code path can send two exits for one trade or record PnL twice.

#### C5 — Reconciler  [P0] [L]
- [x] Done
- **Deviation:** entries/opens younger than 60 s are skipped (may be mid-flight in submit_order); CLOSING age is tracked in memory (first seen), since the table has no closing timestamp; adopted fills go through new `OrderManager.adopt_entry_fill` (atomic `TradeRepo.open_from_fill`); recently closed symbols (<2 min) are not flagged UNMANAGED_POSITION; a missing broker position also cancels any leftover stop order; new setting `reconcile_interval_seconds`. MockBroker `cancel_order` now only cancels WORKING orders.
- **Fixes:** F11
- **Files:** new `backend/app/core/execution/reconciler.py`, `backend/app/core/engine.py`, `backend/app/brokers/mock/client.py` (test helpers), tests
- **Steps:** `Reconciler(broker, session_factory, order_manager, cache, *, clock, is_market_open)` with `async run_once() -> ReconcileReport` (counts per rule). It does nothing while the broker is disconnected. Rules, in order:
  1. **PENDING older than 2 min:** if `find_order_by_ref(":entry")` shows a fill → treat as rule 2; otherwise → CANCELLED.
  2. **SUBMITTED:** look up `:entry`. FILLED/PARTIAL → record the fill → OPEN → publish `trade_executed` → protective stop (C6). WORKING older than 5 min → cancel it if the market is open (the next run resolves it). REJECTED or not found and older than 5 min → CANCELLED.
  3. **OPEN:** compare the broker position for the symbol with the trade's `filled_quantity` (D4 guarantees one trade per symbol).
     - Broker quantity 0 → the position was closed outside the normal path. Using `order_manager.finalize_close` (C4 step 8): `get_execution_summary(":stop")` → finalize `STOP_LOSS`; else `:exit` → finalize with the stored `exit_reason`; else finalize `EXTERNAL` at the last cached price (fallback: entry price) and alert `POSITION_CLOSED_EXTERNALLY`.
     - Broker quantity smaller than DB → alert `QUANTITY_MISMATCH` (no automatic action).
  4. **CLOSING older than 2 min:** broker quantity 0 → finalize as in rule 3. Otherwise, if the market is open and `close_attempts < 3` → `close_position(id, <stored exit_reason>, allow_from_closing=True)`. Otherwise alert `CLOSE_NEEDS_MANUAL_INTERVENTION` once.
  5. **Broker positions with no active DB trade:** alert `UNMANAGED_POSITION` once per symbol per day. Never auto-close them (they may be the user's manual trades).
- Deduplicate alerts in memory by `(alert_type, key, trading_date)`. Log one INFO summary per run.
- Engine: run once after start (when connected), register it as a broker reconnect listener (B2), and schedule `PeriodicTask("reconciler", 120, reconciler.run_once)`.
- **Tests (integration with MockBroker helpers `set_position`, `set_order_state`):** one test per rule, including: a stop filled while the app was down → STOP_LOSS with the stop's price; a SUBMITTED trade whose order filled → OPEN; an unmanaged position → one alert across two runs.
- **Done when:** after any crash or outage, the DB matches the broker within one reconciler run.

#### C6 — Broker protective stop (RSK-07)  [P1, required before live] [M]
- [x] Done
- **Deviation:** `OrderManager(protective_stops=..., stop_cancel_wait_seconds=5)` constructor args (engine passes `settings.broker_protective_stops`); `place_protective_stop` is public and idempotent (reconciler reuses it); a stop still WORKING after the cancel wait aborts the close with CLOSE_FAILED (no sell); IBKR `cancel_order` refreshes open orders once if the id is unknown. `dead broker_stop` variable was already gone.
- **Fixes:** F14
- **Files:** `backend/app/core/execution/order_manager.py`, `backend/app/brokers/ibkr/client.py`, `backend/app/brokers/mock/client.py`, tests
- **Steps:**
  1. After an entry fill, if `settings.broker_protective_stops` or `request.submit_stop_to_broker`: place `STP SELL filled_quantity @ stop_loss_price`, `tif="GTC"`, `order_ref=f"{id}:stop"`; save `stop_order_id`. On failure, publish `PROTECTIVE_STOP_FAILED` (the internal monitor still protects the trade).
  2. `close_position` cancels the stop before any market exit (C4 step 3). After `cancel_order`, poll `find_order_by_ref(":stop")` for up to 5 s until it is no longer WORKING; if it is FILLED, finalize as STOP_LOSS and do **not** send a market sell (that would create a short).
  3. Trailing stops stay internal-only in this version; do not modify the broker stop. The broker stop is a catastrophic backstop at the original risk-approved price.
  4. Remove the now-dead `broker_stop` variable in `submit_order`.
  5. MockBroker: STP orders stay WORKING; add test helper `trigger_stop(order_ref, price)` that fills one and updates the position.
- **Tests:** an entry places exactly one stop with the filled quantity; a manual close cancels the stop and then sells; a stop that already filled at the broker → the close finalizes STOP_LOSS with no market order; the reconciler finalizes a stop that filled while the app was down (C5 rule 3).
- **Done when:** every OPEN trade has a resting stop at IBKR (check in TWS/Gateway during the G1 soak).

#### C7 — Circuit breaker: day rollover, durability, events  [P0] [M]
- [x] Done
- **Deviation:** `rehydrate` does not set `killed` (follows the pattern: kills are durable through `strategies.is_enabled`, so a streak of 5 on a re-enabled strategy is not re-killed until the next loss); extra read-only breaker properties (`trading_date`, `daily_loss_limit_pct`, `halt_threshold_amount`, `strategy_streaks()`); `TradeRepo.closed_since` / `recent_pnls_by_strategy` return tuples (the class has a `list` method that shadows the builtin in annotations); `TradingEngine.rehydrate_circuit_breaker()` runs before the first reconcile.
- **Fixes:** F16
- **Files:** `backend/app/core/risk/circuit_breaker.py`, `backend/app/core/execution/order_manager.py`, `backend/app/core/engine.py`, `backend/app/api/v1/risk.py`, tests
- **Steps:**
  1. Use `market_calendar.trading_date(now)` (ET) instead of the UTC date everywhere.
  2. `is_halted(*, now=None)` returns `self._halted_today and self._current_day == trading_date(now)`.
  3. `record_closed_trade(...)` returns a list of newly triggered events: `"DAILY_HALT"` the first time the halt trips today, `"STRATEGY_KILLED"` the first time a strategy is killed. Existing callers may ignore it.
  4. `rehydrate(realized_pnl_today, day_starting_balance, recent_pnls_by_strategy, now=None)` (pattern). The engine calls it at start: today's realized PnL = sum of `pnl` for trades closed since today's ET midnight; starting balance = current net liquidation minus that sum (if the broker is not connected, use the largest `account_balance_at_entry` among today's closed trades); recent PnLs per strategy = the last `max_consecutive_losses` closed trades, newest first.
  5. OrderManager handles events after a close: `DAILY_HALT` → alert `CIRCUIT_BREAKER_HALT`; `STRATEGY_KILLED` → `StrategyRepo.set_enabled(id, False)` (the durable kill) + alert `STRATEGY_AUTO_DISABLED`.
  6. Admin API: `kill` also disables the strategy in the DB; `reset-strategy` clears the streak only (re-enable via PATCH); `reset` (daily halt) stays in memory. A restart re-computes the halt from the DB, so an admin reset does not survive a restart. That is the safe direction; say so in the endpoint docstring.
  7. `GET /risk/circuit-breaker` adds `trading_date`, `daily_loss_limit_pct`, `halt_threshold_amount`, and per-strategy `consecutive_losses`.
  8. Update existing breaker tests that assume a UTC-midnight rollover (19:00–24:00 ET is still the same trading day).
- **Pattern:**
  ```python
  def rehydrate(self, *, realized_pnl_today: Decimal, day_starting_balance: Decimal,
                recent_pnls_by_strategy: dict[UUID, list[Decimal]], now: datetime | None = None) -> None:
      self._current_day = market_calendar.trading_date(now)
      self._day_starting_balance = day_starting_balance
      self._daily_realized_pnl = realized_pnl_today
      threshold = day_starting_balance * self._daily_loss_limit_pct
      self._halted_today = day_starting_balance > 0 and realized_pnl_today <= -threshold
      for strategy_id, pnls in recent_pnls_by_strategy.items():
          streak = 0
          for pnl in pnls:
              if pnl >= 0:
                  break
              streak += 1
          self._strategies[strategy_id] = _StrategyState(consecutive_losses=streak)
  ```
- **Tests:** a halt from yesterday is not active today with no new trades; `rehydrate` with today's loss ≥ 3% → halted; an auto-kill disables the strategy row (integration); a restart (new breaker + `rehydrate`) keeps today's halt.
- **Done when:** a restart never lifts a halt, and a halt never outlives its trading day.

#### C8 — Single-instance engine lock  [P1, required before live] [S]
- [x] Done
- **Deviation:** the engine acquires the lock once synchronously in `start()` (so a leader is running when `start()` returns) and then a background `_lock_loop` renews (10 s) or retries (5 s); a Redis error on renew is tolerated until 2/3 of the TTL has passed since the last success. Leader-only parts: circuit-breaker rehydrate, reconciler, monitor, scheduler, bar refresh. Always on: broker, feed, risk monitor, notifier. `PositionMonitor.stop()` now clears its tracked trades. `engine.is_leader` exposes the state (use it for D6 health).
- **Fixes:** F21
- **Files:** `backend/app/data/cache.py`, new `backend/app/core/engine_lock.py`, `backend/app/core/engine.py`, tests
- **Steps:**
  1. `RedisCache`: `set_if_absent(key, value, ttl) -> bool` (`SET NX EX`), `compare_and_expire(key, value, ttl) -> bool`, `compare_and_delete(key, value) -> bool` (Lua below via `self._client.eval`).
  2. `EngineLock(cache, ttl_seconds=30)` with a random token: `acquire()`, `renew()`, `release()`.
  3. Engine `start()` starts the always-safe parts (broker connection, feed, API-facing state), then waits in a background task for the lock (retry every 5 s, logging every minute) before starting the trading parts (monitor closes, scheduler, reconciler, jobs). Renew every 10 s; if a renewal fails, stop the scheduler, reconciler and monitor immediately and publish `ENGINE_LOCK_LOST`. `stop()` releases the lock.
- **Pattern:**
  ```python
  _RENEW = "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('expire', KEYS[1], ARGV[2]) else return 0 end"
  _RELEASE = "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end"
  ```
- **Tests (integration, real Redis):** a second lock cannot acquire while the first holds it; it acquires after the first releases; renewal with the wrong token fails.
- **Done when:** two backend processes against the same Redis never both run the scheduler.

#### C9 — One definition of "active risk"  [P1] [S]
- [x] Done
- **Deviation:** `RiskMonitor.open_trade_count` and the snapshot's `open_trade_count` now count all active-risk trades (including PENDING/SUBMITTED/CLOSING).
- **Fixes:** F27
- **Files:** `backend/app/db/models/trade.py`, `backend/app/db/repositories/trade_repo.py`, `backend/app/core/risk/monitor.py`, `backend/app/core/metrics/collector.py`, tests
- **Steps:** add `ACTIVE_RISK_STATUSES = (PENDING, SUBMITTED, OPEN, CLOSING)` next to `TradeStatus`; `get_active_trades()` uses it; `RiskMonitor.check_exposure` and `MetricsCollector.record_snapshot` use `get_active_trades()` (the monitor's `open_trade_count` becomes the active count). `PositionMonitor` keeps using OPEN only.
- **Tests:** a CLOSING trade counts toward the portfolio cap and toward the risk monitor's exposure.
- **Done when:** the dashboard's exposure equals the cap OrderManager enforces.

---

### Phase D — Operator controls and alerting

#### D1 — Pause and resume automated trading  [P1] [S]
- [x] Done
- **Fixes:** F19 (part)
- **Files:** new `backend/app/core/trading_state.py`, new `backend/app/api/v1/trading.py` (register the router in `main.py`), `backend/app/core/engine.py`, `backend/app/core/strategy_engine/scheduler.py`, tests
- **Steps:**
  1. `TradingState(cache)`: Redis key `trading:paused` (`"1"` = paused). `is_paused()` returns **True if Redis is unreachable** (fail safe for entries). `pause(reason)`, `resume()`; both log at WARNING.
  2. Pause blocks **automated entries only**. Exits, the monitor, the reconciler, and manual orders (D3) keep working.
  3. Wire `is_paused` into the scheduler (B7 step 4).
  4. `GET /trading/status` → `paused`, `market_open`, `in_eval_window`, `broker_connected`, `engine_lock_held`, `scheduler_last_cycle_at`, `open_trade_count`, `circuit_breaker_halted`. `POST /trading/pause` (body `{reason}`) and `POST /trading/resume`.
- **Tests:** paused → the scheduler never loads strategies; Redis down → treated as paused; status endpoint shape.
- **Done when:** the user can stop new automated trades with one call, and the pause survives restarts (Redis persistence comes in F3).

#### D2 — Manual close and flatten  [P1] [S]
- [x] Done
- **Fixes:** F19 (RSK-06 "manual close records MANUAL")
- **Files:** `backend/app/api/v1/trades.py`, `backend/app/api/v1/trading.py`, tests
- **Steps:**
  1. `POST /trades/{id}/close` → `engine.order_manager.close_position(id, "MANUAL")`; map `CloseOutcome` to HTTP: CLOSED 200, PENDING_EXIT 202, NOT_OPEN 409, MARKET_CLOSED 409, FAILED 502. Return the trade.
  2. `POST /trading/flatten` with body `{"confirm": "FLATTEN"}` (anything else → 422): pause automated trading, then close every OPEN trade one at a time; return a per-trade outcome list.
- **Tests:** manual close → CLOSED with `exit_reason=MANUAL`; a second close → 409; flatten without the confirm string → 422; flatten closes all OPEN trades and leaves trading paused.
- **Done when:** the user can exit any position or everything from the API (UI in E3/E4).

#### D3 — Manual order entry  [P1] [M]
- [x] Done
  - Deviation: also implements E3/E6 backend parts (`GET /trades/active`, `GET /signals`, `GET /symbols/{ticker}/bars`).
- **Fixes:** F19 (manual trades; also covers the paper-account keep-alive trade)
- **Files:** `backend/app/api/v1/trades.py`, `backend/app/api/v1/schemas.py`, tests
- **Steps:** `POST /trades` with body `{symbol, stop_loss_price, take_profit_price?, quantity?}` (prices as strings):
  1. 409 if the market is closed or there is no fresh price key for the symbol.
  2. The entry price is the fresh price key; the balance comes from the broker (409 if disconnected).
  3. Quantity defaults to `RiskCalculator.max_quantity(...)`; if given, it must be ≥ 1.
  4. Build a `TradeRequest(strategy_id=None, ...)` → `engine.order_manager.submit_order(...)`; `RiskRejectionError` → 422 with the reason; return the created trade with status 201.
- **Tests:** a valid request opens a trade through the full risk pipeline; a stop above entry → 422 `INVALID_STOP_LOSS`; quantity over the 1% limit → 422 `RISK_LIMIT_EXCEEDED`; a symbol with an active trade → 422 `SYMBOL_ALREADY_ACTIVE`.
- **Done when:** a manual trade is validated, audited and monitored exactly like a strategy trade.

#### D4 — Alerts everywhere; error.log aggregation  [P1] [M]
- [x] Done
  - Deviation: `SCHEDULER_FAILING` was missing and is added (counts cycle-level exceptions only; per-symbol errors are caught inside the cycle). Alert counts for D5 are in memory.
- **Fixes:** F25, F15 (notifier)
- **Files:** `backend/app/monitoring/logger.py`, `backend/app/notifications/dispatcher.py`, `backend/app/core/engine.py`, the modules listed below, tests
- **Steps:**
  1. `setup_logging`: create **one** `RotatingFileHandler` for `error.log` (level ERROR, `JsonFormatter`, `MaskingFilter`) and attach it to `trading`, `risk`, `system`, and `error` loggers. Refactor `_make_logger` to accept an existing file handler so `error.log` has exactly one writer.
  2. Make sure every alert in this table goes through `publish_system_alert` (most were added by earlier tasks; add any missing):

     | alert_type | Raised by |
     |------------|-----------|
     | BROKER_DISCONNECTED / BROKER_RECONNECTED / BROKER_ACCOUNT_MODE_MISMATCH | B2 |
     | PRICE_FEED_STALE | B4 |
     | DATA_REFRESH_FAILED | B6 |
     | ORDER_STUCK_WORKING | C2 |
     | CLOSE_FAILED / CLOSE_PARTIAL / CLOSE_NEEDS_MANUAL_INTERVENTION | C4, C5 |
     | POSITION_CLOSED_EXTERNALLY / QUANTITY_MISMATCH / UNMANAGED_POSITION | C5 |
     | PROTECTIVE_STOP_FAILED | C6 |
     | CIRCUIT_BREAKER_HALT / STRATEGY_AUTO_DISABLED | C7 |
     | ENGINE_LOCK_LOST | C8 |
     | SCHEDULER_FAILING (3 consecutive cycles raised) | scheduler |

  3. Notifier: the engine consumes `risk_updates`, `trade_events`, `system_alerts` through `supervised_subscribe`. The dispatcher throttles identical `(alert_type, symbol)` alerts to once per 15 minutes and sends `severity == "INFO"` alerts by email only (no SMS).
- **Tests:** a `system_logger.error` call lands in `error.log` (temp `log_dir`); throttling suppresses a duplicate within 15 minutes; the notifier survives a simulated Redis disconnect.
- **Done when:** every failure mode the bot can't fix by itself reaches the user's inbox/phone.

#### D5 — Daily summary and end-of-day snapshot  [P2] [S] *(parallel-safe)*
- [x] Done
  - Deviation: per-type alert counts and halt/kill counts are in-memory (reset after each summary and on restart); added `TradeRepo.opened_since`.
- **Fixes:** F33
- **Files:** new `backend/app/core/jobs/daily_summary.py`, `backend/app/notifications/dispatcher.py`, `backend/app/core/engine.py`, tests
- **Steps:** at `daily_summary_time_et` on trading days (pattern like B6's `tick`): write a `PortfolioSnapshot` via `MetricsCollector`, then email a summary: realized PnL today, trades opened/closed today, open positions with unrealized PnL (cached price vs `entry_fill_price`), halts/kills today, alert counts by type. Persist the last-run date in Redis (`job:daily_summary:last_run`) so a restart doesn't send it twice.
- **Tests:** runs once per trading day; not on holidays; the snapshot row is written.
- **Done when:** the user gets one email per trading day; a missing email means something is wrong.

#### D6 — Health and status  [P1] [S] *(parallel-safe)*
- [x] Done
  - Deviation: engine without the lock reports `error` (HTTP 503) with the info fields still present.
- **Fixes:** deployment health checks
- **Files:** `backend/app/api/v1/system.py`, `backend/app/main.py`, tests
- **Steps:** keep `/health` as **liveness only** (no dependencies; the Docker healthcheck uses it, so IB's nightly maintenance never restarts the container). Add an `engine` component to `/api/v1/system/health`: `ok` when the engine is started and holds the lock; detail fields `feed_subscriptions`, `max_tick_age_seconds` (over symbols with open trades), `scheduler_last_cycle_at`, `tracked_trades`, `paused`, `market_open`. Lower the per-check log lines in this endpoint from INFO to DEBUG.
- **Tests:** engine missing → `engine.status == "error"` and HTTP 503; `/health` is 200 even with the broker disconnected.
- **Done when:** one request tells the user whether the bot is actually trading.

#### D7 — Keep backtests off the trading event loop  [P1] [S] *(parallel-safe)*
- [x] Done
  - Deviation: the semaphore wraps the whole job; a queued job stays `pending` until its turn.
- **Fixes:** F31
- **Files:** `backend/app/api/v1/backtesting.py` (only `_run_job` and `_run_portfolio_job`)
- **Steps:** run each engine in a worker thread with its own event loop — `result = await asyncio.to_thread(asyncio.run, engine.run(...))` — and allow one backtest at a time with a module-level `asyncio.Semaphore(1)`. Do not change any other backtesting code.
- **Tests:** existing backtesting tests pass; a test that runs a backtest while a 50 ms periodic coroutine runs asserts the coroutine keeps ticking.
- **Done when:** a running backtest no longer delays stop checks.

---

### Phase E — Frontend

#### E1 — Live event store  [P1] [M]
- [x] Done
- **Fixes:** F22
- **Files:** new `frontend/src/store/liveStore.ts`, `frontend/src/hooks/useWebSocket.ts` → rename to `useDashboardSocket.ts`, `frontend/src/components/layout/Layout.tsx`, `frontend/src/pages/Dashboard.tsx`, dashboard components
- **Steps:**
  1. Zustand store (pattern). `useDashboardSocket()` is mounted **once** in `Layout.tsx` and calls `useLiveStore.getState().applyEvent(event)` for every message, so no event is ever dropped.
  2. Remove every `lastEvent` prop. Components read selectors: a ticker's price via `useLiveStore((s) => s.prices[ticker])`; queries re-fetch via `useEffect(() => { queryClient.invalidateQueries(...) }, [tradesVersion])`.
- **Pattern:**
  ```ts
  interface PriceTick { price: number; changePct: number | null; at: string }
  interface LiveState {
    prices: Record<string, PriceTick>;
    tradesVersion: number;
    riskVersion: number;
    alerts: DashboardEvent[];
    socketState: number;
    applyEvent: (e: DashboardEvent) => void;
    setSocketState: (s: number) => void;
    dismissAlert: (index: number) => void;
  }

  export const useLiveStore = create<LiveState>((set) => ({
    prices: {}, tradesVersion: 0, riskVersion: 0, alerts: [], socketState: WebSocket.CONNECTING,
    applyEvent: (e) => set((s) => {
      const p = e.payload as Record<string, string | null>;
      switch (e.event) {
        case "price_update":
          return { prices: { ...s.prices, [String(p.symbol)]: {
            price: Number(p.price), changePct: p.change_pct ? Number(p.change_pct) : null, at: e.timestamp } } };
        case "trade_executed":
        case "trade_closed":
          return { tradesVersion: s.tradesVersion + 1 };
        case "risk_alert":
          return { riskVersion: s.riskVersion + 1, alerts: [e, ...s.alerts].slice(0, 50) };
        case "system_alert":
          return { alerts: [e, ...s.alerts].slice(0, 50) };
        default:
          return {};
      }
    }),
    setSocketState: (socketState) => set({ socketState }),
    dismissAlert: (i) => set((s) => ({ alerts: s.alerts.filter((_, j) => j !== i) })),
  }));
  ```
- **Done when:** the watchlist shows live prices from the backend envelope, and the socket indicator reads from the store.

#### E2 — Types and API client  [P1] [S]
- [x] Done
- **Files:** `frontend/src/types/api.ts`, `frontend/src/api/client.ts`
- **Steps:** `TradeStatus` = `"PENDING" | "SUBMITTED" | "OPEN" | "CLOSING" | "CLOSED" | "CANCELLED"`; `ExitReason` = `"STOP_LOSS" | "TAKE_PROFIT" | "MANUAL" | "STALE_TIMEOUT" | "EXTERNAL"`; add the C1 trade fields; add types `StrategyType`, `StrategySignal`, `TradingStatus`, `CircuitBreakerStatus`, `CloseOutcome`. In `request()`: if the response is not JSON (e.g. an nginx 502 page), return `{data: null, meta: ..., error: {code: "HTTP_<status>", message: <text>}}` instead of throwing.
- **Done when:** `npm run typecheck` passes with the new backend fields used.

#### E3 — Active trades table  [P1] [M]
- [x] Done
- **Files:** backend `GET /api/v1/trades/active` in `backend/app/api/v1/trades.py` (thin route over `get_active_trades()`); `frontend/src/components/dashboard/ActiveTradesTable.tsx`
- **Steps:** list all active statuses with a status badge. Columns: Symbol, Status, Qty (filled), Entry (fill), Stop (`current_stop_price ?? stop_loss_price`), Target, Last (store), Unrealized P&L = (last − entry) × qty (green/red), Risk $, Opened. Add a **Close** button on OPEN rows → confirm dialog → `POST /trades/{id}/close` → show the outcome (202 = "exit pending"). Refetch when `tradesVersion` changes, and every 60 s as a fallback.
- **Done when:** a CLOSING trade is visible, and a trade can be closed from the dashboard.

#### E4 — Trading controls, circuit breaker panel, alerts feed  [P1] [M]
- [x] Done
- **Files:** new `frontend/src/components/controls/TradingControlsPanel.tsx`, new `frontend/src/components/risk/CircuitBreakerPanel.tsx`, new `frontend/src/components/alerts/AlertsFeed.tsx`, `frontend/src/pages/Dashboard.tsx`, `frontend/src/pages/SystemHealth.tsx`
- **Steps:**
  1. Controls (top of Dashboard): a status chip (Running / Paused / Halted / Broker disconnected / Market closed) from `GET /trading/status` (refetch every 15 s); Pause/Resume; **Flatten all**, which requires typing `FLATTEN` in a dialog.
  2. Circuit breaker panel (Dashboard): today's realized PnL vs the halt threshold as a bar; halted state with **Reset halt** (confirm); a per-strategy list of loss streaks with **Reset**.
  3. Alerts feed: newest first from the store, colored by severity (CRITICAL red, WARNING amber, INFO gray), each dismissible.
- **Done when:** every D1/D2/C7 control is usable without curl.

#### E5 — Create strategies; schema-driven config forms  [P2] [M]
- [x] Done
- **Files:** new `frontend/src/components/strategies/NewStrategyDialog.tsx`, new `frontend/src/components/strategies/SchemaForm.tsx`, `frontend/src/components/strategies/StrategyConfigForm.tsx`, `frontend/src/pages/Strategies.tsx`
- **Steps:** a "New strategy" button opens a dialog: type picker from `GET /strategies/types`, a name field, `SchemaForm` → `POST /strategies`. `SchemaForm` renders integer/number/string/enum fields, a multi-select of watchlist symbols for `symbols`, and a JSON textarea for object or array-of-object fields (composite `strategies`). Replace the hardcoded schemas in `StrategyConfigForm` with the types endpoint. Show 422 messages inline.
- **Done when:** the frontend contains no hardcoded strategy schema.

#### E6 — Symbol detail page  [P2] [M]
- [x] Done
- **Fixes:** F34
- **Files:** backend: `GET /symbols/{ticker}/bars?days=365` (`OHLCVRepo.get_bars`) in `symbols.py`; new `backend/app/api/v1/signals.py` with `GET /signals?symbol=&strategy_id=&limit=` (`SignalRepo.list`). Frontend: new `frontend/src/components/charts/PriceChart.tsx`, `frontend/src/pages/SymbolDetail.tsx`.
- **Steps:** a candlestick chart with `lightweight-charts` **v4 API** (the repo pins `^4.2.0`): `createChart(el, options)`, `chart.addCandlestickSeries()`, `series.setData([{ time: "2026-10-19", open, high, low, close }])`, `series.setMarkers(...)` for signals and trade entries/exits. Below it: a live price from the store, a signals table, and a trades table (`/trades?symbol=`).
- **Done when:** the page shows a real chart with signal markers.

---

### Phase F — Deployment

#### F1 — Backend image  [P1] [S]
- [ ] Done
- **Fixes:** F23 (backend)
- **Files:** `backend/Dockerfile`, new `backend/.dockerignore`, `docker-compose.yml` (dev)
- **Steps:**
  1. `.dockerignore`: `.env`, `.env.*`, `logs/`, `tests/`, `.pytest_cache/`, `.ruff_cache/`, `__pycache__/`, `*.pyc`.
  2. Dockerfile (pattern): no build toolchain (every dependency ships wheels; if `pip install` ever fails to build a wheel, restore `build-essential`), non-root user, migrations then a **single** uvicorn process.
  3. Dev `docker-compose.yml`: bind Postgres and Redis to `127.0.0.1` only (`"127.0.0.1:5432:5432"`, `"127.0.0.1:6379:6379"`). Leave its other dev behavior as is.
- **Pattern:**
  ```dockerfile
  FROM python:3.11-slim
  ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
  WORKDIR /app
  COPY requirements.txt .
  RUN pip install --no-cache-dir -r requirements.txt
  COPY . .
  RUN useradd --create-home --uid 10001 appuser && mkdir -p /app/logs && chown -R appuser /app/logs
  USER appuser
  EXPOSE 8000
  CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port 8000"]
  ```
- **Done when:** `docker build ./backend` succeeds and `docker run --rm <image> ls -a` shows no `.env`.

#### F2 — Frontend image and nginx  [P1] [S]
- [ ] Done
- **Fixes:** F23 (frontend)
- **Files:** new `frontend/nginx.conf`, `frontend/Dockerfile`, new `frontend/.dockerignore` (`node_modules`, `dist`), `.gitignore` (`deploy/nginx/.htpasswd`)
- **Steps:** copy `nginx.conf` to `/etc/nginx/conf.d/default.conf` in the Dockerfile. Create the password file on the host with `docker run --rm httpd:2.4-alpine htpasswd -nbB <user> <password> > deploy/nginx/.htpasswd` (never commit it).
- **Pattern:**
  ```nginx
  server {
    listen 80;
    auth_basic "Trading Bot";
    auth_basic_user_file /etc/nginx/.htpasswd;
    root /usr/share/nginx/html;
    index index.html;

    location /api/ {
      proxy_pass http://backend:8000;
      proxy_set_header Host $host;
      proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
      proxy_read_timeout 60s;
    }

    location /ws/ {
      proxy_pass http://backend:8000;
      proxy_http_version 1.1;
      proxy_set_header Upgrade $http_upgrade;
      proxy_set_header Connection "upgrade";
      proxy_set_header Host $host;
      proxy_read_timeout 3600s;
    }

    location / {
      try_files $uri /index.html;
    }
  }
  ```
- **Done when:** deep links like `/strategies` load after a browser refresh, and the dashboard WebSocket connects through nginx.

#### F3 — Production compose stack  [P1] [M]
- [ ] Done
- **Fixes:** F23
- **Files:** new `docker-compose.prod.yml`, new `.env.prod.example`, `.gitignore` (`.env.prod`)
- **Steps:**
  1. Write the stack below. Facts verified on 2026-10-19 from the `gnzsnz/ib-gateway-docker` README: other containers reach the Gateway at `ib-gateway:4004` (paper) or `:4003` (live); 4001/4002 listen only inside the container; `READ_ONLY_API` must be `"no"` or orders are rejected; `AUTO_RESTART_TIME` restarts daily without 2FA; tag `10.50` is the current stable line.
  2. Pin TimescaleDB to the version you already run: `docker compose exec db psql -U trading -d trading_bot -tc "select extversion from pg_extension where extname='timescaledb'"` → use `timescale/timescaledb:<that version>-pg15`.
  3. `.env.prod.example` lists only key names: `SECRET_KEY`, `POSTGRES_PASSWORD` (URL-safe characters only, since it is embedded in `DATABASE_URL`), `IBKR_USERNAME`, `IBKR_PASSWORD` (paper credentials), `VNC_PASSWORD`, `NOTIFICATION_EMAIL_SMTP`, `NOTIFICATION_SMS_TWILIO`, plus optional risk settings from A1.
  4. Run with `docker compose -f docker-compose.prod.yml --env-file .env.prod up -d --build`.
  5. Alternative if the user keeps running IB Gateway natively on their Mac: delete the `ib-gateway` service and set `IBKR_HOST: host.docker.internal`, `IBKR_PORT: "4002"`. On an Apple Silicon Mac, if the Gateway image has no arm64 variant, add `platform: linux/amd64` to that service.
- **Pattern:**
  ```yaml
  name: trading-bot

  x-logging: &default-logging
    driver: json-file
    options: { max-size: "10m", max-file: "5" }

  services:
    ib-gateway:
      image: ghcr.io/gnzsnz/ib-gateway:10.50
      restart: always
      environment:
        TWS_USERID: ${IBKR_USERNAME}
        TWS_PASSWORD: ${IBKR_PASSWORD}
        TRADING_MODE: paper
        READ_ONLY_API: "no"
        TWOFA_TIMEOUT_ACTION: restart
        RELOGIN_AFTER_TWOFA_TIMEOUT: "yes"
        AUTO_RESTART_TIME: "11:59 PM"
        TIME_ZONE: America/New_York
        TZ: America/New_York
        VNC_SERVER_PASSWORD: ${VNC_PASSWORD}
      ports: ["127.0.0.1:5900:5900"]
      networks: [internal]
      logging: *default-logging

    db:
      image: timescale/timescaledb:<pinned>-pg15
      restart: unless-stopped
      environment:
        POSTGRES_USER: trading
        POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
        POSTGRES_DB: trading_bot
      volumes: [postgres_data:/var/lib/postgresql/data]
      healthcheck:
        test: ["CMD-SHELL", "pg_isready -U trading -d trading_bot"]
        interval: 10s
        timeout: 5s
        retries: 5
      networks: [internal]
      logging: *default-logging

    redis:
      image: redis:7-alpine
      restart: unless-stopped
      command: ["redis-server", "--appendonly", "yes"]
      volumes: [redis_data:/data]
      healthcheck:
        test: ["CMD", "redis-cli", "ping"]
        interval: 10s
        timeout: 5s
        retries: 5
      networks: [internal]
      logging: *default-logging

    backend:
      build: ./backend
      restart: unless-stopped
      env_file: .env.prod
      environment:
        ENVIRONMENT: production
        BROKER: ibkr
        IBKR_HOST: ib-gateway
        IBKR_PORT: "4004"
        IBKR_TRADING_MODE: paper
        DATABASE_URL: postgresql+asyncpg://trading:${POSTGRES_PASSWORD}@db:5432/trading_bot
        REDIS_URL: redis://redis:6379/0
        LOG_DIR: /app/logs
        CORS_ORIGINS: "[]"
      volumes: [app_logs:/app/logs]
      depends_on:
        db: { condition: service_healthy }
        redis: { condition: service_healthy }
        ib-gateway: { condition: service_started }
      healthcheck:
        test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"]
        interval: 30s
        timeout: 5s
        retries: 3
        start_period: 60s
      networks: [internal]
      logging: *default-logging

    frontend:
      build: ./frontend
      restart: unless-stopped
      ports: ["127.0.0.1:8080:80"]
      volumes: ["./deploy/nginx/.htpasswd:/etc/nginx/.htpasswd:ro"]
      depends_on: [backend]
      networks: [internal]
      logging: *default-logging

  networks:
    internal:

  volumes:
    postgres_data:
    redis_data:
    app_logs:
  ```
- **Done when:** the stack starts from scratch, `/api/v1/system/health` reports everything ok after the Gateway logs in, and nothing except `127.0.0.1:8080` and `127.0.0.1:5900` is published.

#### F4 — Backups  [P1] [S] *(parallel-safe)*
- [ ] Done
- **Files:** new `scripts/backup.sh`, `.gitignore` (`backups/`)
- **Steps:** a daily backup (run from host cron at 17:30 ET on weekdays) that keeps 14 days of database dumps and audit-log copies. TimescaleDB restores need `SELECT timescaledb_pre_restore();` before `pg_restore` and `SELECT timescaledb_post_restore();` after; document that in F6.
- **Pattern:**
  ```sh
  #!/usr/bin/env sh
  set -eu
  cd "$(dirname "$0")/.."
  STAMP=$(date -u +%Y%m%dT%H%M%SZ)
  mkdir -p backups
  docker compose -f docker-compose.prod.yml exec -T db pg_dump -U trading -d trading_bot -Fc > "backups/trading_bot_${STAMP}.dump"
  docker compose -f docker-compose.prod.yml cp backend:/app/logs/audit.log "backups/audit_${STAMP}.log"
  find backups -type f -mtime +14 -delete
  ```
- **Done when:** a restore into a scratch database from last night's dump succeeds.

#### F5 — CI additions  [P2] [S] *(parallel-safe)*
- [ ] Done
- **Files:** `.github/workflows/ci.yml`
- **Steps:** add a job that runs `docker build ./backend` and `docker build ./frontend`; make sure `tests/integration/test_engine_wiring.py` (B9) runs in the existing backend job (it will, since the job runs all tests).
- **Done when:** a broken Dockerfile fails CI.

#### F6 — Runbook and doc fixes  [P2] [S]
- [ ] Done
- **Fixes:** F35
- **Files:** new `DEPLOYMENT.md`, `.claude/specs/ibkr-gateway.md`, `.claude/specs/environment-setup.md`, `.claude/specs/design.md`, `README.md`, `.claude/specs/tasks.md`
- **Steps:**
  1. `DEPLOYMENT.md` (short, imperative): first boot; approving 2FA through the VNC tunnel (`ssh -L 5900:localhost:5900 host`); weekly Gateway re-authentication; daily checks (the summary email, `/api/v1/system/health`); using pause / manual close / flatten; backup and restore (with the Timescale steps); upgrading (pull, rebuild, `up -d`; migrations run on start); rollback (previous image + restore).
  2. Fix port and broker-selection drift in the specs; add the event contract (§3.3) and the background-task table (§3.4) to `design.md`; update the README quick start for both compose files; add a "Phase 7 — see production-readiness-plan.md" pointer at the end of `tasks.md`.
- **Done when:** a new reader can deploy and operate the bot from `DEPLOYMENT.md` alone.

#### F7 — Optional: AWS host  [P3] [L]
- [ ] Done (only if the user chooses AWS in D1)
- **Steps:** follow the user's global instructions: load the `aws-cdk`, `aws-secrets-manager` and `aws-compute` skills first, use infrastructure as code (CDK, Python), and never put em dashes in AWS resource names or descriptions. Shape: default VPC; one EC2 instance (t3.small, Amazon Linux 2023, Docker); **no inbound security-group rules**; SSM Session Manager for shell and port forwarding (`AWS-StartPortForwardingSession` for 8080 and 5900); encrypted gp3 volume with daily snapshots (Data Lifecycle Manager); an IAM role with `AmazonSSMManagedInstanceCore` and write access to one S3 backup bucket; secrets in Secrets Manager rendered into `.env.prod` at boot with the `{{resolve:secretsmanager:...}}`/`asm-exec` pattern from the skill (secret values must never enter the agent's context).

---

### Phase G — Validation

#### G1 — Paper soak and go/no-go  [P0 before live]
- [ ] Done
- Run the production stack against the **paper** account for at least **5 consecutive trading days**. Every day, check and record:
  - [ ] `/api/v1/system/health` ok during market hours; the summary email arrived.
  - [ ] No unexplained entries in `error.log`.
  - [ ] Every symbol with an open trade had ticks all day (no `PRICE_FEED_STALE`).
  - [ ] The reconciler summary shows zero unresolved mismatches.
  - [ ] Every signal in `strategy_signals` produced at most one trade.
  - [ ] Every OPEN trade has a resting GTC stop visible in the Gateway/TWS order list (C6).
  - [ ] Every closed trade has exit fills and a PnL matching IBKR's execution report.
  - [ ] The supervisor log shows a clean reconnect after IB's nightly restart.
- Run these drills once during the soak:
  - [ ] Restart the backend during market hours with a position open; the monitor and reconciler resume and the position stays protected.
  - [ ] Stop the Gateway for 10 minutes; `BROKER_DISCONNECTED` then `BROKER_RECONNECTED` arrive and prices resume.
  - [ ] Pause → no new entries; resume.
  - [ ] Manual close of one trade; flatten-all on the last day.
- **Go-live** needs all of the above green plus the user's explicit approval. Switching to live (separate credentials, `IBKR_TRADING_MODE=live`, `ENVIRONMENT=production`, Gateway `TRADING_MODE: live`, port 4003) is a separate change and out of scope here.

---

### Phase Z — Cleanup (optional)

#### Z1 — Leftover hardening items  [P3] [S]
- [ ] Done
- **Fixes:** F37
- Centralize `min_separation_pct` parsing and validation in `filters.py`; `min_separation_ok` returns `False` when the slower value is ≤ 0; `CompositeStrategy` returns HOLD when the combined stop would be `None`; fetch extra-symbol bars with `asyncio.gather` (after B7 they come from the DB, so this is trivial).

#### Z2 — Persist the trailing stop  [P3] [S]
- [ ] Done
- **Fixes:** F36
- When `_update_trailing_stop` ratchets by at least 0.1R, write `current_stop_price` (C1 column, never the audit `stop_loss_price`); `_track` and `_resync` start from `current_stop_price` when present.

#### Z3 — Repository tidy-up  [P3] [S]
- [ ] Done
- **Fixes:** F38
- Move `test_ibkr_connection.py` to `backend/scripts/check_gateway.py`; split `requirements-dev.txt` (pytest, pytest-asyncio, pytest-cov) from runtime requirements (the Dockerfile installs runtime only, CI installs both); leave the unused `system_logs` table alone unless the user wants it dropped.

---

## Appendix — Migration file template

```python
"""<One-line summary>

Revision ID: 000N
Revises: 000M
Create Date: YYYY-MM-DD
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "000N"
down_revision: str | None = "000M"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    ...


def downgrade() -> None:
    ...
```
