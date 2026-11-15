# Roadmap

Open work only. When something is finished, delete it from this file.

The [architecture review](docs/architecture-review.md) records the November 13, 2026 review of commit `228eadd`. Finding IDs below refer to that evidence; adding a task does not mean its safeguard is implemented. Prioritize execution correctness, shared authorization and trustworthy small-account validation before adding AI or strategy families.

## Before live money (in order)

Everything here runs on the paper account. Settings are in `.env.example`; terms are explained in the README's Growth mode section.

### 0. Repair execution and deterministic authorization

The November 13 review passed 823 backend unit tests, backend lint, frontend lint and the frontend build/type check. Focused mock probes still reproduced the failures below. Add regression coverage for the invariants as each is fixed; passing existing tests is insufficient for live approval.

- [ ] **P1 / AR-03 — Fail closed on recovery and protection faults.** Add shared entry-readiness state in `engine.py` and the execution path. Failed circuit-breaker recovery, missing/stale reconciliation, unresolved mismatches and failed required protective stops must withdraw entry readiness. Done when failure-injection tests block new risk until recovery is confirmed while risk-reducing actions remain available.
- [ ] **P1 / AR-04 — Enforce execution leadership and account reservations.** Cover manual, scheduled and recovery mutations in `dependencies.py`, `order_manager.py`, `engine_lock.py` and repository operations. Reject follower mutations and fence expired leaders immediately before dispatch. Make symbol, cash and risk reservations transactional across processes; continue deploying one worker/replica. Keep automation pause distinct from an account-wide emergency halt. Done when leadership loss, a second process and concurrent proposals cannot create duplicate or over-budget exposure.
- [ ] **P1 / AR-05 — Centralize required data and account checks.** Move mandatory spread/liquidity/freshness checks into shared entry authorization. Preserve source/receipt timestamps and data mode; reject missing required quotes and account facts rather than assuming safe values. Verify settled funds and the broker/account's applicable restrictions. Done when manual and automated entries receive the same deterministic decisions for missing, stale, delayed or invalid required inputs.
- [ ] **P1 / AR-06 — Persist authorization and dispatch durably.** Replace logging-only audit success with checked durable proposal, authorization and order-intent records plus a transactional outbox. Record fills and reconciliation outcomes incrementally, with versioned input references. Preserve existing immutable audit columns and append subsequent facts. Done when failed mandatory persistence prevents dispatch and restart/retry recovers intent without duplicate execution; exercise actual handler I/O failure, not only a mocked logger method.
- [ ] **P1 / AR-07 — Budget all-in loss and actual exposure.** Include estimated entry/exit friction and execution headroom when sizing within the existing fixed 1% ceiling. Maintain current exposure separately from immutable approved risk, including adverse fills and late executions. Done when the $1,000/$10 planned-risk/$1.34 friction case is resized or rejected to meet the all-in modeled budget; report gap risk separately. Entry restrictions must never block an exit because of fees, settlement or day-trade rules.
- [ ] **P2 / AR-10 — Report broker outcomes accurately.** `scheduler_submit.py` records SUBMITTED whenever submission returns without raising, including REJECTED/ERROR results; the manual route can return HTTP 201 for the cancelled trade. Inspect the broker outcome and expose accepted, working, partial, rejected, unknown and filled states to the signal log and API/UI. Done when clean broker rejection and uncertain submission produce truthful outcomes without weakening successful-order behavior.

### 1. Make historical and paper validation representative

- [ ] **P1 / AR-08 — Share simulation and execution policy.** In `backtesting/engine.py`, `portfolio_engine.py` and `backtesting_helpers.py`, apply aggregate capital/cash reservations, growth-universe and position constraints, settlement and relevant exit rules, including signal exits. Advance portfolio slots on a shared calendar. Done when two simultaneous 40-share/$25 positions cannot consume about $2,001 on $1,000, and matching simulated/execution proposals produce explainable equivalent authorization decisions.
- [ ] **P1 / AR-09 — Add an isolated virtual paper ledger.** Keep `TRADING_CAPITAL_LIMIT`'s existing sizing-cap meaning; add explicit experimental cash/equity accounting for small-account certification. Include realized/unrealized P&L, fees, reservations and settlement. Done when a $100 experiment losing $10 has $90 of experimental equity even if broker paper equity remains near $1 million, and subsequent sizing/cash checks use that ledger.
- [ ] **P2 / AR-10 — Correct metrics and chronological validation.** Build marked-to-market equity returns for drawdown and Sharpe in `backtesting/metrics.py`. Extend `walk_forward.py` with rolling evaluations, prior-history warm-up, extra-symbol history and consistent cost settings. Separate training/tuning from untouched evaluation data; use purging/embargo where overlapping labels require it. Done when sparse samples are inconclusive, regime-filter inputs are present, and statistics use their documented sampling period.

### 2. Validate growth mode on paper

1. [ ] Check your IBKR pricing plan (Client Portal: Settings > Account Settings > Pricing) and set `COMMISSION_PLAN`. Validate eligibility using the configured cost model, current prices and actual stop distances; the example equity thresholds in the README are not guarantees of entry.
2. [ ] Set `TRADING_CAPITAL_LIMIT=100` and `ACCOUNT_TYPE=cash` for initial rejection/operational checks. Enable `trend_pullback` with currently affordable ETFs from the configured `GROWTH_UNIVERSE`; check prices and sufficient history for each symbol and its market filter. Use the isolated virtual ledger from section 1 for small-account certification; the sizing cap alone does not reproduce account losses or settlement.
3. [ ] Run one week at 100. Confirm zero-size and fee-ineligible signals are blocked with `ZERO_QUANTITY` or `RISK_REJECTED` / `COSTS_TOO_HIGH`, respectively. Check `growth_mode` and the daily summary against policy. Record no-trade periods as valid outcomes; this interval tests behavior, not profitability.
4. [ ] Run two or more weeks at experimental capital that passes configured fee and whole-share checks. Verify ledger equity/cash, recorded commissions, net P&L, broker-confirmed protective stops and position limits. Reconcile fee estimates with execution reports and the actual pricing plan.
5. [ ] After simulation parity and metric repairs, backtest `trend_pullback` on 5 ETFs over 5 years, starting at $100, $300 and $1,000 on both fee plans. Record dataset versions, symbols, configurations, dates, trade count, turnover, time invested, net expectancy, marked-to-market drawdown, return and fees. Compare with cash and a passive benchmark, and include allocated hosting/data/AI costs. Require informative untouched out-of-sample results at the intended capital; positive in-sample return or a 45% cost-filter pass does not establish an edge.

### 3. Paper soak: 5 trading days in a row

Run the production stack (`scripts/bot.sh up`) on paper with the settings chosen above. This is operational acceptance after the repairs, not proof of profitability. Keep a dated evidence log outside the append-only broker audit log.

Check each day:

- [ ] `/api/v1/system/health` is ok during market hours, and the summary email arrived.
- [ ] No unexplained entries in `error.log`.
- [ ] Every symbol with an open trade had prices all day (no `PRICE_FEED_STALE`).
- [ ] Reconciliation completed recently and shows zero unresolved mismatches, including excess holdings, missing shares, direction, open orders and protection. A healthy component response alone does not establish trading readiness.
- [ ] Every signal in `strategy_signals` produced at most one trade.
- [ ] Every open trade has a resting GTC stop in the Gateway/TWS order list.
- [ ] Every closed trade has exit fills and P&L matching IBKR's execution report.
- [ ] The log shows a clean reconnect after IBKR's nightly restart.

Run once during the soak:

- [ ] Restart the backend during market hours with a position open. The monitor and reconciler resume, and the position stays protected.
- [ ] Stop the Gateway for 10 minutes. `BROKER_DISCONNECTED` then `BROKER_RECONNECTED` arrive, and prices resume.
- [ ] Pause: no automated entries; exits continue under their documented rules. Test the separate account-wide entry halt after implementation, including manual requests. Resume only after checking account consistency.
- [ ] Manually close one trade, and flatten everything on the last day. Verify terminal entry/exit orders, final broker quantities and commissions; a successful HTTP response alone does not prove flatness.
- [ ] Run `scripts/backup.sh` and restore the dump into a scratch database (DEPLOYMENT.md section 6).
- [ ] In an isolated test setup, inject partial fills, cancel races, duplicate delivery, mandatory audit-write failure, unavailable account data, leadership loss and failed startup recovery. Preserve evidence that no forbidden new risk was submitted and every confirmed fill remained accounted for.

### 4. Go live (separate change)

- [ ] Complete the execution, authorization, simulation and paper gates above, plus the account-risk requirements below. Prepare a separate live-deployment review covering account-mode guards, credentials, Gateway configuration, broker/account restrictions, capital limits, operating cost, monitoring and rollback. The current runbook is paper-only; passing validation does not authorize a switch. Any live deployment requires your explicit go-ahead and an approved runbook.

## Leadership architecture delivery

Proposed work toward the supplied hybrid architecture. The [review](docs/architecture-review.md#proposed-architecture) defines boundaries and evidence; these capabilities are not current runtime guarantees.

1. [ ] **Account-risk requirements before live review.** Add deterministic per-position, sector and correlated-exposure limits, marked-to-market daily-loss controls and high-water-mark drawdown controls. Define policy versions and account-level halt/reset semantics. Verify the applicable broker margin regime and missing-value behavior; do not infer account eligibility from a generic day-trade count alone.
2. [ ] **Versioned data and durable events.** Reuse PostgreSQL for decision records and an outbox, keeping Redis for cache/transient UI events. Preserve source/availability/receipt times, data mode, corporate-action adjustment policy and immutable input references. Add fundamentals or news only when the research hypothesis needs them. Done when replay reconstructs the inputs and policy behind a decision without depending on overwritten bars or missed Pub/Sub messages.
3. [ ] **Deterministic ranking and allocation.** Collect proposals before allocation, rank opportunities under an explicit policy and reserve shared capital once. Give strategies read-only data/account interfaces. Done when proposal order cannot silently over-allocate the account and every executable intent references a valid, unconsumed authorization.
4. [ ] **One ML model in shadow evaluation.** Implement features, offline training, model registry, calibration and drift/rollback diagnostics for one model before adding an ensemble. Persist model/feature/data versions and compare with the repaired rule-based baseline. Done when informative out-of-sample results demonstrate incremental benefit after trading and operating costs before influencing allocation.
5. [ ] **Bounded LLM research and explanations.** Begin with cited research/event interpretation and structured outputs, with cached results, timeouts, schema validation, versioned prompts/models and a spending ceiling. Keep broker mutation credentials/capabilities in execution. Done when invalid or unavailable required research blocks the dependent proposal under deterministic policy, and no agent can bypass authorization or reset safeguards.
6. [ ] **Full decision replay and promotion evidence.** Link input snapshots, model/research outputs, proposals, risk evaluations, authorizations, fills, fees and reconciliation. Done when a recorded decision can be reconstructed, duplicate delivery is idempotent and a promotion/rollback decision has reproducible supporting evidence.

Fill accounting and reconciliation are done; the order-state and fill-journal contract lives in `brokers/base.py` and `trade_fills`. For Claude's handoff, another developer can own readiness and authorization; agree on that contract before editing shared execution modules. These are proposed assignments, not evidence of a delivered handoff.

## Optional

Schedule hosting expansion and additional strategy families after the deterministic baseline and validation gates. Reuse infrastructure and set operating-cost limits appropriate to experimental capital.

- [ ] **Show growth mode on the Home page.** `/trading/status` returns `growth_mode` and `frontend/src/types/api.ts` has its type, but nothing displays it. Show a chip with equity and the returned `exit_equity` while `growth_mode.active` is true.
- [ ] **AWS host.** Only if you choose AWS over your Mac or another always-on machine. CDK in Python: default VPC; one t3.small EC2 instance (Amazon Linux 2023, Docker) with no inbound security-group rules; SSM Session Manager for shell and port forwarding (8080, 5900); encrypted gp3 volume with daily snapshots; an IAM role with `AmazonSSMManagedInstanceCore` and write access to one S3 backup bucket; secrets in Secrets Manager, rendered into `.env.prod` at boot.
- [ ] **Strategy cleanup.** Centralize `min_separation_pct` parsing in `filters.py`; make `min_separation_ok` return `False` when the slower value is ≤ 0; make `CompositeStrategy` return HOLD when the combined stop would be `None`; fetch extra-symbol bars with `asyncio.gather`.
- [ ] **Repository tidy-up.** Move `test_ibkr_connection.py` to `backend/scripts/check_gateway.py`. Split `requirements-dev.txt` (pytest, pytest-asyncio, pytest-cov) from the runtime requirements; the Dockerfile installs runtime only, CI installs both.

## Backlog

- **Options (iron condor):** options data feed, multi-leg orders and database models, spread-based risk calculation.
- **Mobile PWA:** manifest and service worker, responsive layout, web push (needs HTTPS).
- **Second broker (Alpaca, Tradier):** new `BaseBroker` implementation and mapper, per-broker paper-mode guard.

### Dividend investing strategy

Build a long-term dividend-income strategy with gradual purchases and optional dividend reinvestment for compounding. Hold through price declines rather than selling on ordinary trading signals; exit individual companies when a dividend cut or omission is confirmed. These are proposed strategy rules to research and validate, not assumptions of guaranteed returns.

- [ ] **Tax research.** Research current dividend taxation for the investor's jurisdiction and account type, including qualified versus ordinary dividends, holding-period requirements, withholding and the treatment of reinvested dividends. Record official sources and the date checked.
- [ ] **Universe and ETF alternative.** Source current S&P 500 Dividend Aristocrats membership and verify the requirement for at least 25 consecutive years of dividend increases. Treat 3M, A. O. Smith (`AOS`), Aflac and AT&T as research examples, not a current approved list. Evaluate ProShares S&P 500 Dividend Aristocrats ETF (`NOBL`) as an alternative to individual stocks; verify its current holdings and expense ratio rather than hard-coding the quoted 0.35%.
- [ ] **Dividend data and eligibility.** Add dividend declarations, amounts, frequency, ex-dividend dates, record dates and payment dates. Calculate dividend yield as annual dividend per share / current share price; distinguish trailing payments from forward annualized dividends and exclude misleading special-dividend annualization. Validate purchase eligibility against the ex-dividend date, not the payment date.
- [ ] **Recurring purchases.** Support a monthly schedule funded by available cash: one share of NOBL per month, or one-twelfth of a planned stock allocation each month for twelve months. Respect whole-share execution and carry unused cash forward. Define an alternative ten-tranche buying ladder with explicit price levels and a total allocation cap; price declines must not trigger unlimited purchases.
- [ ] **Valuation and dividend sustainability.** Research payout ratios, earnings, cash flow and dividend history. Evaluate 2.5%-4.5% yields as a candidate valuation screen, and yields of 6% or more for ordinary companies as a review flag rather than proof of distress or value. Handle REITs and MLPs separately and account for market conditions.
- [ ] **Holding and exit rules.** Keep winners and avoid exits solely because prices fall or technical signals turn bearish. Detect confirmed cuts and omissions without confusing payment-frequency changes, special dividends, splits or ETF distribution variability with a company cutting its dividend. Specify the next-trading-session exit and clarify how the previous day's low sets the order, including gaps and unfilled orders. Define ETF exits separately from individual-company exits.
- [ ] **Portfolio and execution support.** Support repeated purchases, cost basis, dividend cash flows and optional reinvestment. Design long-term holding support around the existing one-active-trade-per-symbol rule, required stops, trailing stops, profit targets and time exits; define strategy-specific behavior while preserving account exposure limits and manual pause, close and flatten controls.
- [ ] **Validation and reporting.** Backtest with historical membership, dividend events, corporate actions, fees and taxes where applicable. Include dividend cuts, deep bear-market drawdowns and cash constraints; prevent survivorship bias and dividend double-counting with adjusted prices. Report dividend income, yield on cost, total return, drawdown and reinvestment separately, then validate on paper.

### Covered calls

Research an income strategy for owned shares in sideways or modestly rising markets. Avoid writing calls on positions selected for sharp appreciation, since the call caps upside while leaving substantial stock downside.

- [ ] **Selection and contract rules.** Define a measurable sideways-market filter and exclusions for strong momentum and upcoming catalysts. Choose strike, actual expiration date, minimum premium after costs, option liquidity and coverage ratio. Reserve enough owned shares for every sold call using the contract's actual deliverable; standard equity contracts generally represent 100 shares. Prevent selling reserved shares while leaving the call uncovered.
- [ ] **Outcomes and assignment.** Model three expiration price cases: below strike (call generally expires, shares remain), near/at strike (assignment is uncertain), and above strike (expect assignment and sale of shares at strike). Calculate combined stock-and-option P&L including premium, stock cost basis and fees. Use the contract calendar rather than assuming every expiration is Friday, and handle early assignment, ex-dividend exposure and after-hours exercise decisions.
- [ ] **Lifecycle and portfolio ownership.** Define buy-to-close, roll, assignment and stock-exit behavior, with linked stock and option positions, broker reconciliation and restart recovery. Keep ownership explicit when shares also belong to dividend or momentum strategies; selling a call must account for the possibility that the shares will be called away.
- [ ] **Dependencies and validation.** Reuse the options feed, contract models and execution support planned for iron condors. Add coverage checks, assignment events and reporting of net premium, total return and capped upside. Backtest sideways, rally and selloff periods with realistic spreads and fees, then validate on paper.

Reference: [OIC covered calls](https://www.optionseducation.org/strategies/all-strategies/covered-call-buy-write) and [assignment mechanics](https://www.optionseducation.org/referencelibrary/faq/options-assignment).

### Momentum stocks: separate long and short strategies

Define momentum as sustained directional price movement over a specified period. Research rapid revenue growth and young technology or other growth companies as optional selection factors; company age or industry alone does not establish momentum.

- [ ] **Long momentum.** Define lookback, trend strength, relative strength, liquidity and volume thresholds; specify buy entries, initial stops, trailing exits, failed-trend exits and maximum holding time. Compare with existing `stock_trend` and `trend_pullback` behavior before deciding what to reuse.
- [ ] **Short momentum.** Specify whether entries follow sustained downward momentum or a confirmed breakdown after an upward run. Define sell-to-open and buy-to-cover rules, stops above entry, trailing exits, squeeze/gap handling and maximum holding time. Do not short solely because a stock has risen sharply or has a high valuation.
- [ ] **Short-selling infrastructure.** Implement directional risk sizing, P&L, position tracking and reconciliation before enabling short entries; short selling is currently unsupported by the risk manager. Account for borrow availability, borrow fees, recalls, margin, dividend obligations and broker restrictions in execution and simulation.
- [ ] **Validation.** Backtest long and short strategies separately across market regimes, using information available at each decision date and realistic costs. Report hit rate, realized average win/loss, expectancy, total return and drawdown; validate both entry and exit rules on paper.

### Rocket stocks: high-volume breakout strategy

Research a long momentum variant targeting rapid appreciation over weeks or months. Treat the proposed breakout rules as hypotheses to validate rather than a promise of wealth.

- [ ] **Price and trend rules.** Require a breakout to a new 52-week high and a new all-time high, with price above the 50-day moving average and the 50-day moving average above the 200-day moving average. Define intraday versus completed-bar confirmation, sufficient price history and split-adjusted comparisons; calculate prior highs without including future data.
- [ ] **Volume confirmation and gaps.** Require a configurable surge relative to prior average volume. Flag gaps to new highs with unusually high volume for evaluation; define entry timing, maximum gap/extension and slippage limits rather than buying every alert. Compare intraday volume at equivalent times of day if using live partial bars.
- [ ] **Valuation and exits.** Evaluate the proposed approach without a low-P/E entry requirement; test whether valuation adds useful information rather than assuming it never matters. Define initial stop, position size, trailing exit, failed-breakout exit and a weeks-to-months holding limit, including overnight and earnings gaps.
- [ ] **Validation.** Compare with general long momentum and existing trend strategies. Include failed breakouts, delisted stocks, fees and slippage; use out-of-sample testing and paper validation before enabling execution.

### Put income strategies: covered puts and cash-secured puts

- [ ] **Covered put research.** Evaluate short stock plus a sold put as the bearish counterpart to a covered call. Specify share coverage, strike, expiration, net premium, borrow costs and assignment-driven purchase of shares to close the short. Model limited gains and potentially unlimited loss if the stock rises; this depends on both options support and the short-selling infrastructure above.
- [ ] **Cash-secured put research.** Evaluate selling a put while reserving enough cash to buy the shares at the strike if assigned. Define eligible stocks the portfolio is willing to own, strike/expiration selection, cash reservation, assignment and subsequent holding or exit rules. This is a stock-acquisition/income sibling of covered calls, distinct from a covered put backed by short stock; account for substantial losses if the stock falls.
- [ ] **Validation and reporting.** Model early assignment, exercise/expiration, collateral changes, commissions, spreads and portfolio exposure for each strategy separately. Compare total return and drawdown with covered calls and stock ownership, and validate on paper.

References: [OIC covered puts](https://www.optionseducation.org/strategies/all-strategies/covered-put) and [cash-secured puts](https://www.optionseducation.org/strategies/all-strategies/cash-secured-put).
