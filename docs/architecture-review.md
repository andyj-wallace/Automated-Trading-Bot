# MVP architecture review: hybrid AI stock trading

Review performed November 13, 2026; documented November 14, 2026.
Code baseline: `228eadd4c6e12d068cbf54bf717d887ebf5dd15b`.
Scope: leadership's supplied hybrid architecture, current implementation, small-capital economics,
and an incremental delivery plan. This document records findings and proposals; it does not
implement safeguards or authorize live deployment. Open implementation work belongs in
[ROADMAP.md](../ROADMAP.md); current paper operations belong in [DEPLOYMENT.md](../DEPLOYMENT.md).

## Assessment

Retain the modular backend and repair execution correctness, authorization and validation before
adding predictive AI. The MVP has useful deterministic strategies, broker abstraction, risk
controls, protective-stop support and a paper workflow. Live-trading approval should be withheld
until the release gates below have evidence.

The current system uses rule-based signals. Leadership's ML and LLM capabilities remain future
work. At this scale, separate services for every architecture box would add deployment and
recovery costs. Strong interfaces, durable state and a single execution authority can be built
incrementally in the existing backend; research workers must have restricted capabilities.

## Verification and limits

The original review executed these checks against the baseline above:

| Check | Result |
|---|---|
| `cd backend && python3 -m pytest tests/unit -q` | 823 passed; five runtime warnings involving unawaited test mocks, plus a pytest-asyncio configuration deprecation notice |
| `cd backend && ruff check .` | Passed |
| `cd frontend && npm run lint` | Passed |
| `cd frontend && npm run build` | Passed, including TypeScript compilation |
| Focused mock probes | Reproduced the behaviors described in the findings below |

Local Python was 3.12.2; the declared deployment target is 3.11. Frontend build warnings concerned
outdated Browserslist data and a chart dependency imported both statically and dynamically.
Broker-connected integration tests, load tests, Docker builds and a production paper soak were
not run in this review. Mock probes establish local code-path behavior; they do not establish
real broker execution behavior. They were investigative scripts, not newly committed regression
tests. No broker order was sent and no code was changed during the review.

For the November 14 documentation update, Ruff and the backend unit suite were rerun: all lint
checks passed and all 823 tests passed with the same five runtime warnings. Validation also
checked 46 local links across the four changed Markdown files, Markdown anchors, heading spacing,
code fences and Git whitespace errors. Frontend results above remain the original review results.

## Leadership alignment

| Subsystem | Current implementation | Remaining work |
|---|---|---|
| Market data and research | Streaming prices and bid/ask, daily/intraday bars, exchange calendar, Redis cache, stored OHLCV | Explicit corporate-action treatment, fundamentals/news ingestion, point-in-time datasets, source/availability times and data-quality policy |
| ML signal engine | Indicator strategies and deterministic regime filters | Training/features, predictive model, registry, calibration, drift and rollback diagnostics |
| LLM agent layer | No implemented research, event-analysis or explanation pipeline found | Bounded read-only research, citations, structured outputs and model/prompt/cost tracking |
| Strategy and portfolio | Registry, composite strategies, sizing, sequential evaluation | Account-wide ranking, coordinated allocation and portfolio constraints |
| Independent risk gateway | `RiskManager` and execution-side duplicate, notional, account and growth checks | Consistent centralized checks, readiness, transactional reservations, sector/correlation and drawdown limits |
| Execution and reconciliation | Order lifecycle, broker references, retry lookup, protective stops and position monitoring | Partial-fill correctness, complete quantity reconciliation and failure recovery |
| Observability and audit | Structured logs, signal outcomes, trades, alerts, dashboard and metrics | Checked durable records, full decision lineage, model versions and deterministic replay |
| Historical and paper validation | Single/portfolio backtests, chronological train/test helper, IBKR paper workflow | Policy parity, virtual small-account accounting, rolling evaluation and correct equity-return metrics |

| Core principle | Assessment |
|---|---|
| 1. Modular architecture | Strong foundation: FastAPI routes, strategies, risk, execution, repositories and `BaseBroker` have recognizable responsibilities. |
| 2. Event-driven processing | Partial: ticks and Redis Pub/Sub support monitoring/UI/alerts; strategy decisions run on a schedule. Durable delivery and recoverable decision processing are incomplete. |
| 3. Hybrid AI decision-making | Future work: predictive ML and LLM research/explanations are absent from the reviewed runtime. |
| 4. Deterministic trade authorization | Partial: deterministic checks exist, but entry checks are distributed and authorization is not a durable capability consumed by execution. |
| 5. Independent risk enforcement | Partial: existing strategy signals pass risk checks, but mandatory data checks, process ownership and future AI capability restrictions need stronger boundaries. |
| 6. Autonomous execution | Substantially present for paper trading: qualifying signals can execute without per-trade human approval. Operational reliability remains subject to the findings. |
| 7. Reproducibility and auditability | Partial: signals/trades/logs exist; mutable inputs, ordinary logging and missing version lineage prevent complete deterministic replay. |
| 8. Failure-safe operation | Incomplete: recovery, reconciliation and protection failures do not consistently withdraw entry permission. |
| 9. Backtesting and paper trading | Present with material gaps: capital-policy differences, incomplete exits/settlement, limited chronological validation and unreliable risk-adjusted statistics. |

## Findings

P1 denotes a live-deployment blocker, including small-account certification where specified.
P2 denotes required reporting or architecture work; a lower repair priority does not make
incorrect statistics suitable promotion evidence.

### AR-01: P1 — Partial protective-stop fills can close a trade prematurely

A focused probe supplied a four-share stop fill for a ten-share position. The close path returned
`CLOSED` without submitting an exit for the remaining six. It finalizes when a cancelled stop has
any fill, without establishing that the entire position has exited. Residual exposure can leave
normal trade management and P&L can be misstated.

Combine cumulative stop and exit executions, fees and quantities. Close only when confirmed
exits account for all entry shares; retain management of the remaining quantity through retries.

Evidence: [order_close.py](../backend/app/core/execution/order_close.py#L109).

### AR-02: P1 — Partial-entry remainder state and excess holdings are lost

The broker mapper collapses working and cancelled partial orders into `PARTIAL`. The entry
path transitions either result to `OPEN`. A probe confirmed that behavior. Another probe supplied
four recorded shares and ten broker-held shares; open-position reconciliation reported no
finding. It checks holdings below the expected quantity but has no excess-holdings branch.

Preserve working/terminal order state separately from cumulative fills. Confirm cancellations,
adopt late fills and keep protective quantities aligned. Reconcile excess/missing quantities
and unexpected direction, including after restarts and cancellation races.

Flatten inherits these limitations. The earlier [audit-fixes scope](../specs/audit-fixes.md#out-of-scope)
explicitly accepted best-effort behavior for working partial entries and submissions outlasting
the idle wait. Leadership's stronger emergency-control requirement expands that scope. A pause
or successful flatten response does not prove that all entry orders are terminal or holdings flat.

Evidence: [mapper.py](../backend/app/brokers/ibkr/mapper.py#L178),
[order_entry.py](../backend/app/core/execution/order_entry.py#L117),
[reconciler.py](../backend/app/core/execution/reconciler.py#L166),
[flatten.py](../backend/app/core/execution/flatten.py#L15).

### AR-03: P1 — Recovery and protection failures do not revoke entry readiness

Startup catches circuit-breaker recovery and reconciliation failures and still starts trading
loops. A mock probe confirmed scheduler startup after both failures. Reconciliation may also
return `positions unavailable` without an entry prohibition. Failed protective-stop placement
alerts without establishing a shared halt on further risk-taking.

Add explicit readiness covering recovered risk state, valid leadership, sufficiently recent
reconciliation, resolved mismatches, required account data and protection status. Failure must
prevent additional exposure until recovery is confirmed. Keep diagnostics and risk-reducing
actions available; fees, settlement and day-trade rules must not block exits.

Evidence: [engine.py](../backend/app/core/engine.py#L277),
[reconciler.py](../backend/app/core/execution/reconciler.py#L73),
[order_stops.py](../backend/app/core/execution/order_stops.py#L55).

### AR-04: P1 — Leadership does not cover every execution mutation

`get_engine` checks startup but not leadership. A probe obtained a started follower engine
successfully. Manual entry and close routes use that dependency. The submission lock serializes
reservations only inside one process.

Enforce authority immediately before broker mutations and fence expired leaders. Make account
cash, symbol and risk reservations transactional across processes. Continue deploying exactly
one worker and replica. Preserve the documented automation-pause meaning and add a separate
account-wide emergency entry halt covering manual and automated proposals.

Evidence: [dependencies.py](../backend/app/dependencies.py#L97),
[order_manager.py](../backend/app/core/execution/order_manager.py#L148),
[trades.py](../backend/app/api/v1/trades.py#L107).

### AR-05: P1 — Mandatory data and account checks vary by entry path

A probe confirmed that missing bid/ask data passes the scheduler's spread gate. Spread and
liquidity checks are outside shared authorization, so manual orders receive different checks.
Missing settled cash falls back to general cash balance. Margin eligibility rejects a reported
day-trade count of zero without a complete capability/missing-value policy. Market updates use
receipt time, which cannot establish the age or mode of the underlying information.

Centralize required entry validation. Retain source, availability and receipt timestamps, data
mode and quality. Reject missing required values, verify spendable funds and apply the actual
broker/account restrictions. A missing optional research feature needs an explicit deterministic
policy; it must not silently substitute for required evidence.

Evidence: [scheduler_submit.py](../backend/app/core/strategy_engine/scheduler_submit.py#L41),
[order_records.py](../backend/app/core/execution/order_records.py#L46),
[market_data.py](../backend/app/brokers/ibkr/market_data.py#L60).

### AR-06: P1 — Ordinary logging does not guarantee mandatory audit persistence

Pre-submission audit uses ordinary logging handlers. Standard stream/file handlers can handle
I/O errors internally and return normally. In a failing-stream simulation, the mandatory audit
function returned without raising after its write failed. A mocked logger method that raises
does not cover that handler behavior.

Persist proposal, authorization and order intent with checked success before dispatch. Use a
transactional outbox and append subsequent execution/reconciliation facts. Preserve the existing
immutable authorization columns. Record immutable input snapshots or versioned references:
historical OHLCV currently supports overwrite-on-refresh, so current rows cannot reconstruct
every past decision. An append-mode file is not a complete durable decision journal.

Evidence: [order_manager.py](../backend/app/core/execution/order_manager.py#L335),
[logger.py](../backend/app/monitoring/logger.py#L160),
[ohlcv_bar.py](../backend/app/db/models/ohlcv_bar.py#L18).

### AR-07: P1 — Planned 1% risk excludes friction and later execution changes

The fixed ceiling measures requested quantity times planned entry-to-stop distance. A probe
approved $10 of that risk on $1,000 with $1.34 of estimated round-trip friction: approximately
$11.34 of modeled loss at the stop, before gaps. Actual entry fills can also increase exposure.
The post-confirmation audit calculates fill-based risk, while active risk reservations use the
original approved value.

Size within the existing 1% ceiling using an all-in modeled loss budget and execution headroom.
Maintain current exposure separately from immutable approved risk. Preserve whole-share sizing,
the portfolio hard ceiling and exit availability. A stop does not guarantee an eventual maximum
loss through gaps or execution failures.

Evidence: [manager.py](../backend/app/core/risk/manager.py#L211),
[costs.py](../backend/app/core/risk/costs.py#L48),
[order_manager.py](../backend/app/core/execution/order_manager.py#L375).

### AR-08: P1 — Portfolio simulation can exceed execution's capital limit

A focused simulation opened two simultaneous 40-share positions at approximately $25, totaling
$2,000.80 on $1,000. Execution-side notional validation rejected the equivalent second order.
The position-size calculator limits an individual position's affordability, but simulation
does not enforce the complete shared account allocation policy.

Backtesting also omits growth-universe/position limits, settlement and signal exits. Portfolio
slots advance by bar index, requiring shared-calendar alignment when histories differ. Share
authorization and reservation policy across historical simulation and execution. Match relevant
exit rules and cost assumptions before treating results as promotion evidence.

Evidence: [portfolio_engine.py](../backend/app/core/backtesting/portfolio_engine.py#L149),
[backtesting_helpers.py](../backend/app/api/v1/backtesting_helpers.py#L27),
[order_entry.py](../backend/app/core/execution/order_entry.py#L35).

### AR-09: P1 for small-account certification — A sizing cap does not track experimental equity

`effective_equity` returns the smaller of broker net liquidation and the configured limit.
A probe showed that a $1 million paper account capped at $100 still supplies $100 after losing
$10. That experiment cannot reproduce the depletion of a $100 account or its isolated cash
and settlement constraints.

Keep the existing cap's documented meaning. Add a separate virtual account ledger with starting
capital, realized/unrealized P&L, commissions, reservations and settlement. After a $10 loss, the
$100 experiment must use $90 of experimental equity for subsequent decisions.

Evidence: [growth.py](../backend/app/core/risk/growth.py#L11).

### AR-10: P2 — Outcomes and statistics can mislead promotion decisions

The scheduler records `SUBMITTED` whenever submission returns without raising, including a
clean rejection. Manual creation can return HTTP 201 for the resulting cancelled trade.
Expose accepted, working, partial, rejected, unknown and filled outcomes accurately.

Backtest Sharpe annualizes per-trade observations with a daily factor. Drawdown uses closed-trade
P&L and the initial balance rather than a marked-to-market peak-equity curve. Correct the account
return sampling period and mark open holdings to market before ranking strategies.

The chronological helper supplies one train/test split with heuristics; it does not implement
rolling evaluation or forward extra-symbol history into its backtests. That matters for separate
regime symbols. Add prior-history warm-up, consistent cost settings, rolling evaluations and
appropriate leakage controls. Sparse out-of-sample trade counts remain inconclusive.

Evidence: [scheduler_submit.py](../backend/app/core/strategy_engine/scheduler_submit.py#L152),
[trades.py](../backend/app/api/v1/trades.py#L159),
[metrics.py](../backend/app/core/backtesting/metrics.py#L40),
[walk_forward.py](../backend/app/core/backtesting/walk_forward.py#L86).

## Small-capital economics

The following values were recomputed with the repository's cost model: a hypothetical $25
instrument, a 2% stop, a 2R target and 5 bps slippage per leg. They are model illustrations,
not current instrument prices, guaranteed broker charges or minimum starting balances.

| Capital | Shares | Tiered round-trip friction | Tiered break-even win rate | Fixed round-trip friction | Fixed break-even win rate |
|---:|---:|---:|---:|---:|---:|
| $100 | 2 | $0.77 | 59.0% | $1.05 | 68.3% |
| $250 | 5 | $0.87 | 44.9% | $2.13 | 61.7% |
| $500 | 10 | $1.03 | 40.2% | $2.25 | 48.3% |
| $1,000 | 20 | $1.34 | 37.8% | $2.50 | 41.7% |

The configured 45% break-even ceiling filters costs; it does not predict a strategy's win rate
or prove that its target reward will be realized. Actual eligibility depends on price, stop
distance, spendable funds, pricing plan and fills. Include trading costs and allocated operating
expenses when measuring net expectancy, without double-counting spread/slippage assumptions.

IBKR's published US base schedule supports the model's $0.0035/share and $0.35 minimum for
Tiered, and $0.005/share and $1 minimum for Fixed, subject to caps and applicable fees. Reconcile
actual charges with execution reports. [IBKR commissions](https://www.interactivebrokers.com/en/pricing/commissions-stocks.php),
checked November 13, 2026.

Retain whole-share sizing for this integration. IBKR documents restrictions on Lite API orders
and fractional stock trading through these APIs.
[IBKR Lite API FAQ](https://www.interactivebrokers.com/support/faq/content/76128095),
[IBKR fractional-share guidance](https://www.interactivebrokers.com/campus/trading-lessons/fractional-shares/),
checked November 13, 2026.

A hypothetical $10/month hosting/data/AI bill consumes $120/year, or 12% of $1,000 before trading
costs. Batch and cache research, reuse infrastructure and set explicit spending limits. Start
with a narrow liquid long-only universe and evaluate the existing daily strategy against cash
and a passive benchmark. Record turnover, time invested, net expectancy, drawdown and dollar
profit after operating expenses. Holding cash is an acceptable outcome when no proposal passes.

FINRA adopted replacement intraday-margin standards effective June 4, 2026, with broker phase-in
through October 20, 2027. Account support must reflect the broker's actual applicable regime;
the review did not establish which regime applies to the user's account.
[FINRA Regulatory Notice 26-10](https://www.finra.org/rules-guidance/notices/26-10), checked November 13, 2026.
Cash experiments need the applicable T+1 calendar and verified spendable funds.
[SEC settlement guidance](https://www.investor.gov/introduction-investing/general-resources/news-alerts/alerts-bulletins/investor-bulletins/new-t1-settlement-cycle-what-investors-need-know-investor-bulletin),
checked November 13, 2026.

## Proposed architecture

AI models generate intelligence and proposals. Deterministic systems validate, authorize,
execute and monitor. No research agent may bypass the risk gateway, reset safeguards or mutate
broker/account state outside execution.

| Component | Responsibility |
|---|---|
| Data adapters | Normalize inputs and preserve source, availability/receipt times, mode, quality, adjustment policy and version. |
| ML/research workers | Produce versioned signals and cited evidence with bounded runtime/spending and read-only capabilities. |
| Proposal/allocation engine | Rank opportunities and allocate available account capital under deterministic policy. |
| Risk gateway | Validate every entry against current account state, reservations, readiness and policy. |
| Execution engine | Consume authorized intents, own broker mutations and track orders and individual executions. |
| Reconciler | Establish account consistency and withdraw entry readiness when consistency is uncertain. |
| Decision journal | Preserve inputs, outputs, authorizations, executions, fees and replay references. |

An authorization should reference the proposal, policy version, account snapshot, market-data
snapshot, quantity, permitted price bounds, expiry and reservation. Execution rejects expired
or consumed authorization. Preserve original authorization facts and append current execution
facts rather than rewriting audit-immutable values.

PostgreSQL decision records and a transactional outbox fit the existing stack. Redis can continue
handling cache and transient UI delivery. This proposal does not require separate services for
each subsystem or a new streaming platform. Broker mutation credentials/capabilities stay in
execution; strategies and research receive read-only data/account interfaces.

Add sector/correlated exposure, per-position concentration, marked-to-market daily-loss and
high-water-mark drawdown controls. The current daily circuit breaker measures realized losses.
Define deterministic missing-input, halt/reset and recovery policy before enabling AI influence.

Introduce one ML model after the baseline and validation environment are trustworthy. Require
incremental out-of-sample benefit after trading and operating costs before it influences
allocation. Persist model, feature and dataset versions, calibration and drift/rollback evidence.

Start LLM work with cited research, event interpretation and explanations. Cache outputs and
persist prompt/model versions, structured responses, evidence references and cost. Bound time
and spending. Required research failure blocks the dependent proposal according to deterministic
policy. Optional-feature degradation must be explicit and evaluated; explanations do not grant
trade authority.

## Delivery and Claude handoff

These are proposed work packages for the requested 12:20 a.m. handoff. Claude was not contacted
and no assignment or availability was confirmed. Agree on order-state and journal contracts
before two developers edit shared execution modules.

| Order | Work package | Acceptance evidence |
|---|---|---|
| 1 | Fill accounting and reconciliation; proposed first package for Claude | Every execution accounted for; residual quantities managed; protection matches holdings. |
| 2 | Readiness, leadership and reservations; proposed independent developer workstream | Failed recovery and expired leadership block entries; concurrent proposals cannot oversubscribe capital. |
| 3 | Durable authorization and execution journal | Crash recovery reconstructs intent; failed mandatory persistence prevents dispatch; retries are idempotent. |
| 4 | Simulation parity and virtual small-account ledger | Equivalent historical/paper proposals produce explainable policy decisions and account evolution. |
| 5 | Metrics, outcomes and operating controls | Correct marked-to-market statistics, accurate broker outcomes, emergency halt and visible growth constraints. |
| 6 | ML/LLM extensions and full replay | Reproducible incremental benefit, bounded cost and no agent broker mutation access. |

Defer options, short selling, more strategy families and optional cloud expansion while repairing
the baseline. The existing strategy backlog carries additional lifecycle, data and capital
requirements. Proposed future features do not change current guardrails or dependency policy.

## Release evidence

Implement the open acceptance tasks in the roadmap, then retain evidence for:

- Partial entries/exits, late fills, cancellation races and correct protective quantities.
- Duplicate delivery, process competition, expired leadership and atomic capital reservations.
- Mandatory persistence failure, unavailable account/market data and failed startup recovery.
- Restart/disconnect recovery with all confirmed executions still accounted for.
- Simulation/execution policy parity, virtual account depletion and settlement.
- Reproducible out-of-sample comparisons with sufficient observations and realistic total costs.
- Production paper operation, broker reconciliation, notifications and a scratch restore exercise.

A five-trading-day soak demonstrates operational behavior after repairs. It does not establish
profitability. Live deployment remains a separate change requiring explicit approval and a
reviewed runbook; approving this documentation does not authorize execution-code changes or live
broker configuration.
