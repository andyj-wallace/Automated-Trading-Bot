# Spec: dividend-data

## Goal
Give the bot a stored, trustworthy history of dividend payments (including declared future ones) from Alpha Vantage's free DIVIDENDS API, plus pure functions that compute dividend yield and detect confirmed dividend cuts. This is the data foundation for the later dividend holdings ledger and recurring-buy work (ROADMAP.md, "Strategy families", Phase 2, tasks 2 and 3). It places no orders and changes no trading behavior. It is for the single operator of this paper-trading bot.

## User stories / requirements
- As the operator, I want dividend events (ex-dividend, declaration, record and payment dates, and amount) stored per symbol so later features can use them.
- As the operator, I want the daily refresh to stay inside Alpha Vantage's free limit of 25 requests per day so it never gets the key blocked.
- As the operator, I want to add the API key to `.env` later and have everything work with no key until then.
- R1 (setting): `app/config.py` gets `alpha_vantage_api_key: str = ""` (env `ALPHA_VANTAGE_API_KEY`), plus `dividend_refresh_daily_budget: int = 20` (validated 1 to 25) and `dividend_symbols: list[str] = []`. Empty key means the refresh job does nothing and logs once per day. The key value is never logged or included in an exception message or `extra`.
- R2 (client): a client in `app/data/` calls `GET https://www.alphavantage.co/query?function=DIVIDENDS&symbol=<S>&apikey=<K>` with `httpx` (already a dependency; add none), a timeout, and one retry on a network error. The HTTP transport is injectable so tests never use the network. A reply is valid only if the JSON has a `data` list. Any other reply (a message body, non-JSON, HTTP error) is a failure that returns an error result and never raises into the caller.
- R3 (parsing): each row has `ex_dividend_date`, `declaration_date`, `record_date`, `payment_date` and `amount` (all strings; dates are `YYYY-MM-DD`). Amount becomes `Decimal` built from the string. A missing, empty, `"None"` or unparseable date is stored as NULL. A row with no valid ex-dividend date or no valid positive amount is dropped and counted. Rows come newest first and include declared future rows. A row whose ex-date is after today (injectable clock, ET) counts as declared, not paid.
- R4 (storage): new table `dividend_events` via hand-written migration `backend/alembic/versions/0013_dividend_events.py` (`revision = "0013"`, `down_revision` = the current head, a working `downgrade()`), model in `app/db/models/`, repository in `app/db/repositories/`. Unique on (symbol, ex_dividend_date). Columns: symbol, ex_dividend_date, declaration_date, record_date, payment_date, amount (Numeric, no float), fetched_at (UTC). A second table or column records per-symbol `last_fetched_at` for successful fetches. Upserting an existing row updates its dates and amount. Rows that disappeared from a later reply are kept.
- R5 (budget): a request counter (Redis, keyed by the ET trading date, expiring after 48 hours) caps requests per day at `dividend_refresh_daily_budget`. It is checked and incremented before each request; a request that would exceed the budget is not sent. Failed requests still count. The refresh job processes symbols oldest-`last_fetched_at` first (never-fetched first), skips a symbol fetched successfully in the last 7 days unless an ex-dividend date stored for it has passed since that fetch, stops when the budget is spent, and logs the skipped symbols. A failed reply never writes rows and never updates `last_fetched_at`.
- R6 (job): a `core/jobs/` refresh job, run once a day through the existing periodic-task mechanism (`PeriodicTask` / `spawn()` from `app/core/tasks.py`) and started where the other jobs are started. It takes injectable client, repository, cache and clock. With `dividend_symbols` empty or no key it does nothing.
- R7 (yield): `app/core/dividends.py` has `trailing_yield(events, price, today)` (sum of paid amounts with ex-date in the last 365 days divided by price) and `forward_yield(events, price, today)` (the latest regular amount times the observed payments per year, divided by price). Both use `Decimal`, return `None` when price is not positive or there is not enough history, and exclude special dividends. A special dividend is an event whose amount is at least 2.5 times the median of the regular amounts in the prior 4 events, or that falls less than 45 days after the previous event on a quarterly or longer cycle. `frequency(events)` derives payments per year (1, 2, 4 or 12) from the median gap in days, not from the API.
- R8 (cut detection): `detect_cut(events, today, grace_days=45)` returns `CUT`, `OMISSION` or `NONE`. `CUT`: the latest regular (non-special) amount, normalised for frequency, is below the previous regular amount by more than a tolerance (default 1%). A frequency change (for example quarterly to monthly with a proportionally smaller amount) is not a cut when the annualised amount did not fall. `OMISSION`: the expected next ex-date (last regular ex-date plus the median gap) is more than `grace_days` in the past with no later event stored. Fewer than 4 events returns `NONE`. Splits: an amount drop whose ratio matches a split on the same date is not a cut; since no split data exists here, a drop to exactly 1/N for N in 2..10 followed by amounts continuing at that new level is treated as a split, not a cut, and this assumption is documented in the docstring.

## Acceptance criteria
Checkable statements only: each one is a test that passes, a command and its result, or an observable behavior.
- [ ] AC1: `cd backend && ruff check .` exits 0.
- [ ] AC2: `cd backend && python3 -m pytest tests/unit -q` exits 0, including the new tests below, and no existing test was edited except to add the new settings.
- [ ] AC3: a test parses a saved sample reply (a trimmed copy of the real shape: `{"symbol": "IBM", "data": [{"ex_dividend_date": "2026-11-10", "declaration_date": "2026-10-21", "record_date": "2026-11-10", "payment_date": "2026-12-10", "amount": "1.69"}, ...]}`) into events with `Decimal` amounts and the correct dates, marks the future-dated row as declared, and a row with `"None"` for a date stores NULL for it.
- [ ] AC4: tests show that a reply without a `data` list, a non-JSON body, an HTTP error status and a timeout each produce an error result, write no rows and leave `last_fetched_at` unchanged.
- [ ] AC5: a test runs the refresh job over 30 symbols with a budget of 20 and asserts exactly 20 requests are made, the 10 skipped symbols are logged, and the next day's run starts with the skipped ones.
- [ ] AC6: with an empty `alpha_vantage_api_key` the job makes zero requests and does not raise; a test asserts that the key value never appears in captured log output when a key is set and a request fails.
- [ ] AC7: a test asserts the second refresh of an unchanged symbol within 7 days sends no request, and that a stored ex-date that has passed since the last fetch makes it eligible again.
- [ ] AC8: tests for `trailing_yield`, `forward_yield` and `frequency` cover a quarterly payer, a monthly payer, a special dividend (excluded from both yields), price `0` (returns `None`) and fewer than 4 events.
- [ ] AC9: tests for `detect_cut` cover: a real cut, an unchanged dividend, an increase, a quarterly-to-monthly change with equal annual total (`NONE`), a special dividend (`NONE`), a 1-for-N split-like drop (`NONE`), an omission past the grace period (`OMISSION`), within the grace period (`NONE`), and fewer than 4 events (`NONE`).
- [ ] AC10: migration `0013` upgrades and downgrades cleanly against a `*_test` database, and `alembic heads` shows a single head.
- [ ] AC11: no network call is made by any test (the HTTP transport is faked), and no file changes `backend/requirements*.txt`.
- [ ] AC12: README documents the new settings and what the job does; ROADMAP.md Phase 2 task 2 and task 3 are ticked or deleted per the project's roadmap rules.

## Constraints
- Follow `.claude/CLAUDE.md`: `Decimal` from `str`, no `float` for money; timezone-aware UTC datetimes and ET through `app/core/market_calendar.py`; loggers from `app.monitoring.logger` with structured `extra` that never uses a reserved `LogRecord` key; keep task references from `spawn()` / `PeriodicTask`; routes stay thin (this change adds no routes); database access only through repositories.
- Do not touch the do-not-touch files in AGENTS.md. In particular do not edit `.env.example`; the new setting names are documented in the README and added to `.env.example` by the operator.
- New constructor parameters default to "off".
- Never read or print `.env` or `.env.prod`.
- No new dependency. Do not change fee constants or any trading, risk or execution code.
- Alpha Vantage is the only data source. Keep the client small and replaceable behind a tiny interface so a different source could be swapped in.

## Out of scope
- The holdings ledger, recurring buys, exit rules, the dividend strategy and its backtests (later Phase 2 tasks).
- Tax research (Phase 2 task 1).
- Any frontend change, any new API route, and any broker or order code.
- Alerting on dividend cuts. Detection only returns a value.

## Open questions
- `.env.example` needs the three new setting names with comments; it is on the do-not-touch list, so the operator adds them by hand after the run.
- Whether the dividend symbols should later come from a watchlist table instead of `dividend_symbols`: none for now.
