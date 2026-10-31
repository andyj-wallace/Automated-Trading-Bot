# Automated Trading Bot — Claude Instructions

Personal trading bot for US stocks and ETFs on IBKR. The user is new to trading, so explain trading terms in plain language.

## Docs

The project keeps at most three docs besides this file. Don't add others.
- `README.md`: what the app does, how it works, local setup, test commands.
- `DEPLOYMENT.md`: running and operating the full stack.
- `ROADMAP.md`: open work only. Check it before suggesting work. Write a new plan there as a self-contained section (ordered tasks, files, steps, tests, "done when"), tick items as you finish, and delete what's finished.

When behavior or a setting changes, update these docs in the same change. Don't cite docs or plan task IDs in code comments.

## Definition of done

1. `ruff check .` and unit tests pass. Integration tests pass if you touched database, Redis, broker or execution code (always use a `*_test` database, see the README). Frontend checks pass if you touched `frontend/`.
2. New behavior has tests.
3. Commit only when the user asks.

## Guardrails: never do these

- Never set `IBKR_TRADING_MODE=live`, connect to Gateway ports 4001/4003, or set the Gateway container to `TRADING_MODE: live` without the user's explicit go-ahead (see ROADMAP.md).
- Never read, print or copy values from `.env`, `backend/.env`, `.env.prod` or `credentials.txt`. Use `.env.example` for key names.
- Never delete or move the user's IB Gateway installers (`ibgateway-*`, gitignored) or local `IB Gateway*/` folder.
- Never modify audit-immutable trade columns after creation: `entry_price`, `stop_loss_price`, `take_profit_price`, `reward_to_risk_ratio`, `risk_amount`, `account_balance_at_entry`. New per-trade facts go in new nullable columns.
- Never delete or rewrite `logs/audit.log`; it is append-only.
- Never make the 1% rule configurable or larger. Never allow the portfolio cap above 10%. Growth mode only adds restrictions.
- Never block, delay or resize an **exit** (stop, target, trailing stop, stale or time exit, manual close, flatten, reconciler) because of growth mode, fees, settlement or day-trade rules.
- Never send fractional quantities to IBKR. `Signal.quantity`, `calculate_position_size()` and `RiskCalculator.max_quantity()` stay `int`.
- Never implement short selling, or send take-profit orders to the broker.
- Never run more than one uvicorn worker or backend replica.
- Never use `float` for money in the backend; build `Decimal` from `str`.
- Never call `asyncio.create_task(...)` without keeping a reference. Use `spawn()` / `PeriodicTask` from `app/core/tasks.py`.
- Never use a reserved `LogRecord` attribute as a key in `extra={...}` (`message`, `asctime`, `msg`, `args`, `name`, `levelname`, `levelno`, `pathname`, `filename`, `module`, `lineno`, `funcName`, `created`, `msecs`, `relativeCreated`, `thread`, `threadName`, `process`, `processName`, `exc_info`, `exc_text`, `stack_info`, `taskName`). Python raises `KeyError` and the log call never runs.
- Never add a dependency without asking.
- Never change the fee constants in `app/core/risk/costs.py` from memory. They are IBKR's published US stock rates as of 2026-10.

## Not bugs: don't "fix" these

- `CircuitBreaker.record_closed_trade()` needs no lock: it is synchronous, and asyncio can't interleave inside it.
- A break-even close (`pnl == 0`) resets the consecutive-loss streak on purpose.
- The DB query inside `OrderManager._submission_lock` is what makes the portfolio cap race-free.
- The bot holding cash for days or weeks at small equity is the fee check working.
- Trend Pullback decides from completed daily bars and enters the day **after** the reversal bar, which keeps live trading and backtests identical.
- `TRADING_CAPITAL_LIMIT` changes sizing and checks only. The Portfolio page and snapshots still show the real broker account.

## Conventions

- Routes stay thin; logic lives in `app/core`; database access only through `app/db/repositories`; responses use `ok()` / `err()` from `app/api/v1/schemas.py`.
- All broker calls go through `BaseBroker`; no broker-specific logic outside `app/brokers/`.
- Money and prices are `Decimal`, serialized as strings in JSON.
- Times are timezone-aware UTC `datetime`. Market-hours logic goes only through `app/core/market_calendar.py`.
- Log with `trading_logger`, `risk_logger`, `system_logger`, `error_logger`, `audit_logger` from `app.monitoring.logger`, with structured `extra={...}`.
- Anything that depends on "now" or market hours takes injectable callables: `clock: Callable[[], datetime] = market_calendar.now_et`, `is_market_open: Callable[[], bool] = market_calendar.is_market_open`.
- New constructor parameters default to "off" (`None`, `0`, `False`), so existing callers and tests keep today's behavior.
- A new setting goes in `app/config.py` and `.env.example` with a comment. Secrets and required values have no default in code.
- Tests: `pytest.ini` sets `asyncio_mode = auto`. Reuse existing fakes such as `FakeCache`, `FakeSymbolRepo`, `FakeTradeRepo` and the fixtures in `tests/integration/test_layer17_e2e.py`.
- Migrations are hand-written: `backend/alembic/versions/000N_<name>.py` with `revision = "000N"`, `down_revision` = previous, and a working `downgrade()`. Next free number: **0011**.
- No comments that restate the code. One short docstring line per public function or class is enough.
