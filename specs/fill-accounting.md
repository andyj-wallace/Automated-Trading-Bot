# Spec: fill-accounting

## Goal
Make the bot account for every share it buys and sells when broker orders fill partly, fill late or fill across trading days (roadmap AR-01 and AR-02). A trade becomes CLOSED only after all its shares have exited, its P&L includes every fill and fee, and the reconciler reports any difference between IBKR's share count and the bot's. This is for the single operator of this paper-trading bot.

## User stories / requirements
- As the operator, I want a protective stop that sells only some shares to leave the rest managed and sold later, so that no shares sit at IBKR without a trade.
- As the operator, I want a partly filled buy order to be finished (cancelled or filled) before the bot relies on its share count, and later fills to be added, so that stops and P&L match what I own.
- As the operator, I want an alert when IBKR holds more shares, fewer shares or a position in the opposite direction compared with what the bot expects.

- R1 (order-state contract): In `backend/app/brokers/base.py`, `OrderResult.status` describes where the order is in its lifecycle, and `filled_quantity` is the cumulative fill:
  - `WORKING`: live at the broker; `filled_quantity` may be > 0.
  - `FILLED`: finished, all shares filled.
  - `PARTIAL`: finished (cancelled, expired or inactive) with some shares filled.
  - `REJECTED`: finished with no shares filled.
  - `ERROR`: state unknown, not finished.

  Add a property `OrderResult.is_terminal` (True for FILLED, PARTIAL and REJECTED) and a field `OrderResult.ordered_quantity: Decimal | None = None` (the total shares the order asked for).

  `map_order_result` in `backend/app/brokers/ibkr/mapper.py`:
  - maps every `_WORKING` IBKR status, including `PendingCancel`, to `WORKING` whatever the fill;
  - keeps `_CANCELLED` mapping to `PARTIAL` when filled > 0, otherwise `REJECTED`;
  - sets `ordered_quantity` from `trade.order.totalQuantity`.

  `MockBroker` follows the same contract:
  - `cancel_order` turns a WORKING order into `PARTIAL` if it has fills, otherwise `REJECTED`;
  - `next_status = "WORKING"` together with `next_filled_quantity` creates a working order with those shares filled and applied to the position;
  - every result carries `ordered_quantity`.

  Every core caller that treated `PARTIAL` as "may still be working" is updated to the new meaning.
- R2 (executions): Add `Execution` to `base.py` with these fields:
  - `exec_id: str`
  - `order_ref: str`
  - `broker_order_id: str`
  - `side: Literal["BUY", "SELL"]`
  - `quantity: Decimal`
  - `price: Decimal`
  - `commission: Decimal | None` (None until the broker reports it)
  - `executed_at: datetime` (UTC, timezone-aware)

  Add an abstract method `BaseBroker.get_executions() -> list[Execution]` that returns every execution the broker currently reports.

  IBKR: a new `mapper.map_executions` converts `reqExecutionsAsync()` fills: `execId`, `orderRef`, `orderId`, `side` (BOT becomes BUY, SLD becomes SELL), `shares`, `price` and `time`. It reads commission from `commissionReport`, using the same "not yet reported" rule as `fills_commission`.

  `MockBroker`:
  - gives every fill a unique `exec_id` and a timestamp;
  - gains a test helper to set a fill's commission;
  - gains `clear_executions()`, which forgets all executions but keeps positions and orders, to simulate a new trading day.

  `get_execution_summary` stays.
- R3 (fill journal): Add the model `TradeFill` in `backend/app/db/models/trade_fill.py`, registered in `app/db/models/__init__.py`. Its table `trade_fills` has these columns:
  - `id`: UUID primary key.
  - `trade_id`: foreign key to `trades.id`, `ON DELETE CASCADE`, indexed, not null.
  - `exec_id`: String(64), unique, not null.
  - `leg`: String(5), not null, with a CHECK constraint limiting it to `ENTRY`, `STOP` and `EXIT`.
  - `side`.
  - `quantity` and `price`: Numeric(18,8).
  - `commission`: Numeric(18,8), nullable.
  - `broker_order_id`: String(32), nullable.
  - `executed_at`: timestamptz, not null.
  - `recorded_at`: timestamptz, server default `now()`.

  Add the hand-written migration `backend/alembic/versions/0012_trade_fills.py` with `revision = "0012"`, `down_revision = "0011"` and a `downgrade()` that drops the table.

  Add `FillRepo` in `backend/app/db/repositories/fill_repo.py`:
  - `record(trade_id, executions) -> int` inserts the executions whose `order_ref` is `{trade_id}:entry`, `{trade_id}:stop` or `{trade_id}:exit`, taking the leg from the suffix. It uses `INSERT ... ON CONFLICT (exec_id)`. On a conflict it only replaces a NULL commission with a reported one and never changes quantity or price. It returns the number of new rows.
  - `totals(trade_id)` returns, for each leg, the quantity, the notional (sum of quantity × price) and the commission (NULL counts as 0).
- R4 (sync): `OrderManager.sync_fills(trade_id, executions=None)` records the trade's executions in the journal and returns the journal totals. It calls `broker.get_executions()` only when `executions` is None.

  When the journal's ENTRY quantity is larger than the trade's stored `filled_quantity` (or that value is NULL), `sync_fills` raises `filled_quantity`, `entry_fill_price` (the journal VWAP) and `entry_commission`. Separately, when the journal's ENTRY commission is larger than the stored `entry_commission` (or that value is NULL) while the quantity is unchanged, `sync_fills` raises `entry_commission` alone. It does this through a new `TradeRepo` method whose SQL never lowers `filled_quantity` or `entry_commission`.

  Definitions used throughout this spec:
  - A trade's entry quantity is `filled_quantity or quantity`.
  - Its open quantity is the entry quantity minus the journal's STOP and EXIT quantities.

  A failed broker call or journal write raises an exception. Callers treat it like any other broker failure: no order is sent based on it.
- R5 (close path, AR-01): `close_position` and `_close_open_trade` in `order_close.py` run these steps in order:
  1. If the entry order `{id}:entry` is WORKING, cancel it and wait for it to become terminal (the same wait as for the stop). Continue even if it does not become terminal.
  2. Cancel the protective stop and wait until `find_order_by_ref` reports it terminal or None. A partly filled stop that is still WORKING is waited for, not treated as gone. If it is still WORKING after the wait, send `CLOSE_FAILED` and sell nothing (as today).
  3. Sync fills.
  4. If a `{id}:exit` order is WORKING, return `PENDING_EXIT` (as today).
  5. If the open quantity is 0 or less, call `finalize_close` and return `CLOSED` when the entry order is terminal or absent. Otherwise send `CLOSE_FAILED` and leave the trade CLOSING.
  6. Otherwise sell `min(open quantity, broker-held shares)` with ref `{id}:exit`. If the broker holds 0 or fewer shares, send `CLOSE_FAILED` (as today). After the result, sync again:
     - open quantity 0 with the entry order terminal: return `CLOSED`;
     - result WORKING: return `PENDING_EXIT`;
     - anything else: the trade stays CLOSING with alert `CLOSE_PARTIAL` (some shares filled) or `CLOSE_FAILED`. A retry sells only what is still open.

  The exit reason is `STOP_LOSS` when the journal's STOP quantity is greater than 0. Otherwise it is the requested reason.
- R6 (finalize): `finalize_close` computes the exit facts from the journal:
  - exited quantity: STOP quantity plus EXIT quantity;
  - `exit_price`: the VWAP of those fills;
  - `exit_commission`: the sum of their commissions;
  - `pnl` for a BUY: exit notional − entry price × exited quantity − `entry_commission` − exit commission. A SELL is mirrored. The entry price is `entry_fill_price or entry_price`.

  The reconciler may finalize a trade after the broker reports no shares even though the journal accounts for fewer shares than the entry quantity. In that case the unaccounted shares are valued at `fallback_price` with no commission and are included in `exit_price` and `pnl`.

  When the journal has no STOP or EXIT fills and `fill` has `filled_quantity > 0`, P&L is computed from `fill` as today. P&L is still recorded exactly once (the CLOSING to CLOSED transition).
- R7 (entry path, AR-02): In `_record_entry_outcome` (`order_entry.py`), the fills of any result with `filled_quantity > 0` are written to the journal.

  A WORKING result with `filled_quantity > 0` is cancelled, and the code waits up to `stop_cancel_wait_seconds` for it to finish:
  - If it becomes terminal, the trade opens with the final cumulative fills.
  - If it is still WORKING, the trade opens with the fills so far, its protective stop covers those shares, and alert `ENTRY_REMAINDER_WORKING` is published.

  `TradeHandler.on_fill` accepts any result with `filled_quantity > 0`. A WORKING result with no fill is handled as today (`ORDER_STUCK_WORKING`, trade stays SUBMITTED).
- R8 (stops follow the open quantity): `place_protective_stop` sizes the stop to the open quantity and places nothing when the open quantity is 0 or less.

  A new `OrderManager` method replaces the broker stop of an OPEN trade that has a recorded `stop_order_id` when either of these holds:
  - `{id}:stop` is missing or terminal while the open quantity is greater than 0;
  - `{id}:stop` is WORKING and `ordered_quantity − filled_quantity` differs from the open quantity.

  Replacement also requires the broker to hold shares in the trade's direction at least equal to the open quantity. It checks this inside the lock, after syncing fills, and places nothing otherwise, so a replacement stop can never sell shares the account does not hold.

  Replacement cancels the old stop and waits until it is terminal, syncs fills, places one stop for the new open quantity at `stop_loss_price`, and publishes `PROTECTIVE_STOP_REPLACED`.

  For any one trade, `close_position`, `place_protective_stop` and stop replacement run under a single per-trade `asyncio.Lock` held inside `OrderManager`, with no nested acquisition. Replacement re-reads the trade inside the lock and does nothing unless the trade is still OPEN.
- R9 (reconciler, AR-02): Each `Reconciler.run_once` fetches executions once and passes them to `sync_fills` for every active trade older than `MIN_AGE`. `OrderManager` methods that the reconciler calls may fetch again after they place or cancel orders.

  PENDING or SUBMITTED trades:
  - An entry order that is WORKING with fills is cancelled quietly and its fills are adopted.
  - A terminal entry order with fills is adopted.
  - The other entry rules are unchanged.

  OPEN trades, in this order:
  1. A journal ENTRY quantity above the stored one is adopted (R4), and alert `LATE_ENTRY_FILL` is published.
  2. An entry order still WORKING after `SUBMITTED_TIMEOUT` while the market is open is cancelled quietly.
  3. Broker-held shares are compared with the open quantity, signed by direction:
     - 0: the existing confirmed-flat rule applies;
     - opposite sign: `QUANTITY_MISMATCH` with `kind="DIRECTION"`;
     - fewer: `kind="MISSING"`;
     - more: `kind="EXCESS"`.
  4. The stop is replaced when R8 applies, and only when step 3 found the broker holding at least the open quantity in the trade's direction (equal, or `EXCESS`). No stop is placed or replaced for a flat broker, `DIRECTION` or `MISSING`.

  Each `QUANTITY_MISMATCH` alert carries `trade_id`, `symbol`, `kind`, `broker_quantity` and `expected_quantity`. It is sent at most once per trade and kind per day. No order is placed to correct a mismatch; the only order step 4 may place is a stop for the open quantity, never more than the broker holds.

  Each run also records the fetched executions into the journal (journal only, no trade columns) for the recently CLOSED trades the reconciler already loads, so a commission report that arrives after the close updates its `trade_fills` row.

  No path marks a trade CLOSED while its entry order `{id}:entry` is still WORKING: R5 steps 5 and 6, flatten and the reconciler all require the entry order to be terminal or absent. When the confirmed-flat rule would finalize a trade whose entry order is still WORKING, the reconciler cancels that entry quietly and leaves the trade as it is; a later run finalizes once the entry is terminal, or adopts the late shares if the remainder filled.

  `_finalize_from_broker` syncs fills and finalizes per R6. Its exit reason is:
  - `STOP_LOSS` if the journal has STOP fills;
  - otherwise `trade.exit_reason or MANUAL` if the journal has EXIT fills;
  - otherwise `EXTERNAL`, and `POSITION_CLOSED_EXTERNALLY` is kept.

  When exits are journaled but cover fewer shares than the entry quantity, it publishes `EXIT_FILLS_MISSING` with the unaccounted quantity.
- R10 (flatten): With R1 in place, `_settle_entry` in `flatten.py` cancels a partly filled entry that is still WORKING and waits until the broker reports it terminal. Only then does it adopt the entry's final cumulative fills (`ENTRY_ADOPTED`). An OPEN trade whose entry remainder is still working is handled by step 1 of R5. All other flatten behavior is unchanged.
- R11 (UI and docs):
  - `frontend/src/components/alerts/AlertsFeed.tsx` gets a plain-language title and step for `ENTRY_REMAINDER_WORKING`, `LATE_ENTRY_FILL`, `PROTECTIVE_STOP_REPLACED` and `EXIT_FILLS_MISSING`.
  - README.md (the Reconciler, Flatten and trade-status paragraphs) and DEPLOYMENT.md (the flatten request and the warning about partial stops and excess holdings) describe the new behavior and drop the limits this change removes.
  - ROADMAP.md deletes the AR-01 and AR-02 items.
  - `.claude/CLAUDE.md` says the next free migration number is 0013.

## Acceptance criteria
- [ ] AC1: `cd backend && ruff check .` exits 0, and `cd backend && python3 -m pytest tests/unit -q` exits 0.
- [ ] AC2: `cd backend && python3 -m pytest tests/integration -q` exits 0 against a `*_test` database. This includes new tests that show:
  - Running `alembic upgrade head` creates `trade_fills`, `alembic downgrade 0011` removes it, and `alembic upgrade head` restores it.
  - Calling `FillRepo.record` twice with the same executions stores one row per `exec_id`.
  - A later non-NULL commission replaces a NULL one.
  - Quantity and price never change.
- [ ] AC3: Unit tests show R1 and R2.
  - Mapper:
    - IBKR `Submitted` with 4 of 10 shares filled maps to `WORKING` with `filled_quantity` 4, `ordered_quantity` 10 and `is_terminal` False.
    - `PendingCancel` with fills maps to `WORKING`.
    - `Cancelled` with 4 filled maps to `PARTIAL` and is terminal.
    - `Cancelled` with 0 filled maps to `REJECTED`.
    - `Filled` maps to `FILLED`.
  - `map_executions` maps BOT to BUY and SLD to SELL, keeps `execId` and `orderRef`, and sets `commission` to None for a report that has not arrived.
  - MockBroker:
    - Cancelling a WORKING order that has fills gives `PARTIAL`.
    - `get_executions` returns unique `exec_id`s.
    - `clear_executions()` empties the execution list and keeps positions.
- [ ] AC4: Integration tests (MockBroker on the test database) show AR-01. They start from a BUY of 10 shares at 10.00 (entry commission 1.00) whose protective stop fills 4 shares at 9.50 (commission 1.00).
  - Full close: `close_position(id, "STOP_LOSS")` sends exactly one SELL market order for 6 shares with ref `{id}:exit`. When that order fills at 9.40 (commission 1.00), the trade is CLOSED with `exit_reason` STOP_LOSS, `exit_price` 9.44, `exit_commission` 2.00 and `pnl` −8.60, and `trade_fills` has 3 rows.
  - Retry after a partial exit:
    1. The exit fills only 3 of the 6 shares at 9.40 (PARTIAL, commission 1.00). The trade stays CLOSING and `CLOSE_PARTIAL` is published.
    2. After `broker.clear_executions()`, a reconciler retry sends one SELL for 3 shares (never 6 or 10).
    3. When that fills at 9.30 (commission 1.00), the trade is CLOSED with `exit_price` 9.41, `exit_commission` 3.00 and `pnl` −9.90, and the broker holds 0 shares.
  - Waiting on a partly filled stop: a stop that is still WORKING with 4 of 10 filled when the cancel is requested is waited on until terminal before any exit is sent.
- [ ] AC5: Tests show the AR-02 entry path. They start from an entry result that is WORKING with 4 of 10 shares filled at 10.00.
  - Cancel confirmed: the trade becomes OPEN with `filled_quantity` 4 and exactly one protective stop for 4 shares.
  - Cancel not confirmed within the wait: the trade becomes OPEN with 4 shares, has one stop for 4 shares, and publishes one `ENTRY_REMAINDER_WORKING` alert.
  - Late fill: the remainder later fills 6 shares at 10.20. A reconciler run then:
    - sets `filled_quantity` to 10 and `entry_fill_price` to 10.12;
    - publishes `LATE_ENTRY_FILL`;
    - cancels the 4-share stop and leaves exactly one working stop for 10 shares.

    `entry_price`, `stop_loss_price` and `risk_amount` are unchanged.
  - Late entry commission: an entry that opens before its commission report arrives (journal commission NULL), then receives a 1.00 commission at an unchanged quantity, has `entry_commission` 1.00 after the next `sync_fills`, and its final `pnl` includes that 1.00.
- [ ] AC6: Reconciler tests show the quantity checks and protection.
  - Mismatch alerts:
    - An OPEN trade expecting 4 shares while the broker holds 10 gives one `QUANTITY_MISMATCH` with `kind` `EXCESS`. It is not repeated on a later run the same day, and no order is placed.
    - Broker −5 gives `DIRECTION`.
    - Broker 6 with no journaled exits gives `MISSING`.
  - Partial stop fill (6 shares stay managed): the broker holds 6 shares, a STOP fill of 4 is journaled, and the stop is still WORKING for the rest. There is no alert, the trade stays OPEN, and no new stop is placed.
  - Stop replacement: the stop then reports PARTIAL (cancelled) with 6 shares open. The reconciler places exactly one new stop for 6 shares and publishes `PROTECTIVE_STOP_REPLACED`.
  - No unsafe replacement: with that terminal stop and 6 shares open, broker holdings of 0, −6 or 3 place no stop (0 follows the confirmed-flat rule; −6 and 3 give `DIRECTION` and `MISSING`), while holdings of 6 or 8 get exactly one stop for 6 shares.
  - Working entry blocks finalization: a CLOSING trade whose entry remainder is still WORKING, with a flat broker on two runs, stays CLOSING and its entry is cancelled. If the remainder then fills 6 shares, the next retry sells exactly those 6 and the trade closes with all 10 shares accounted for.
  - Post-close commission: a commission report that arrives after the trade is CLOSED updates its `trade_fills` row on the next reconciler run, and the trade's `pnl` is unchanged.
  - Incomplete fills at a flat broker: on two runs the broker reports no shares, while the journal has a STOP fill of 4 and nothing else. The trade is CLOSED, `EXIT_FILLS_MISSING` is published, and the other 6 shares are valued at the cached price.
  - Restart: a new `OrderManager` and `Reconciler` over the same database, after `clear_executions()`, still close the trade with P&L that includes the journaled fills.
- [ ] AC7: Flatten and lock tests:
  - Flatten of a SUBMITTED trade whose entry is WORKING with 4 of 10 filled: flatten cancels the entry, adopts it only after the broker reports it terminal, and then closes it with one SELL for the final filled quantity (4 shares, or 10 if the remainder filled during the cancel).
  - Flatten of an OPEN trade whose entry remainder is still WORKING cancels that entry before selling.
  - Lock: a deterministic test (an `asyncio.Event` inside a fake broker's `cancel_order`) runs stop replacement and `close_position` for the same trade concurrently. It shows that the two do not interleave and that, once both finish, no working stop remains for a CLOSING or CLOSED trade.
- [ ] AC8: `cd frontend && npm run typecheck && npm run lint && npm run build` exits 0. `AlertsFeed.tsx` has entries for the four new alert types. README.md, DEPLOYMENT.md and `.claude/CLAUDE.md` contain the R11 changes, and `grep -n "AR-01\|AR-02" ROADMAP.md` prints nothing.

## Constraints
- No new dependencies.
- Follow the `.claude/CLAUDE.md` guardrails:
  - Money is `Decimal` built from `str`.
  - Order quantities sent to the broker are whole shares.
  - No short selling, and no take-profit orders sent to the broker.
  - Audit-immutable trade columns (`entry_price`, `stop_loss_price`, `take_profit_price`, `reward_to_risk_ratio`, `risk_amount`, `account_balance_at_entry`) are never written after creation.
  - No exit is blocked, delayed or resized because of growth mode, fees, settlement or day-trade rules. The cancel waits added here are bounded by `stop_cancel_wait_seconds`.
- Broker-specific code stays in `app/brokers/`. Database access goes only through `app/db/repositories/`. Background work uses `spawn()` / `PeriodicTask`. There is one worker and one replica.
- Existing migration files are not edited, and `0012_trade_fills.py` is the only new migration. These files are not touched: `.env.example`, `.env.prod.example`, `frontend/package-lock.json`, `.github/workflows/ci.yml`, `docs/*.png` and `docs/architecture-review.md`.
- New constructor parameters default to "off". Existing tests may be extended but not weakened. For example, the two `BaseBroker` subclasses in `tests/unit/api/` gain `get_executions`.
- Log `extra={...}` keys never use reserved `LogRecord` attribute names.

## Out of scope
- AR-03 to AR-10: entry readiness and halts, leadership, durable authorization records and the outbox, all-in loss budgeting, simulation parity and outcome reporting. A mismatch alert does not block new entries.
- Flatten's remaining best-effort limit: a strategy entry still inside the broker call after the 30-second idle wait.
- IBKR execution corrections (an `execId` replaced by a corrected execution).
- Executions IBKR no longer reports because the bot was down for the whole day they happened. The reconciler's fallback pricing covers those shares.
- Recomputing a CLOSED trade's `pnl` when a commission report arrives after the close. The journal row is updated; the trade is not.
- Moving the broker stop to the trailing-stop price. Replacement stops use `stop_loss_price`, as today.
- Backfilling the journal for trades opened before this change. Those trades fall back to their stored entry columns.
- Changing API response shapes or the trades page.

## Open questions
- None. The operator decided to cover AR-01 and AR-02 in one spec and to store fills in a journal table keyed by the broker's execution ID.
