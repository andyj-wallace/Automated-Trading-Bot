# Roadmap

Open work only. When something is finished, delete it from this file.

## Before live money (in order)

Everything here runs on the paper account. Settings are in `.env.example`; terms are explained in the README's Growth mode section.

### 1. Validate growth mode on paper

1. [ ] Check your IBKR pricing plan (Client Portal: Settings > Account Settings > Pricing) and set `COMMISSION_PLAN`. On Fixed, the bot starts trading at about $700; switching to Tiered lowers that to about $250.
2. [ ] Set `TRADING_CAPITAL_LIMIT=100` and `ACCOUNT_TYPE=cash`. Enable `trend_pullback` with ETFs from `GROWTH_UNIVERSE` that trade under about $50 (check prices on the Watchlist page; for example SCHB, SCHX, SCHG, SPDW, XLF).
3. [ ] Run one week at 100. Expect **no entries**: `GET /api/v1/signals` shows `RISK_REJECTED` with `COSTS_TOO_HIGH`, or `ZERO_QUANTITY` when one share already risks more than $1. `GET /api/v1/trading/status` shows `growth_mode.active: true` and the daily summary shows the growth line.
4. [ ] Run two or more weeks at `TRADING_CAPITAL_LIMIT=1000` on Fixed (or `300` on Tiered). Expect rare entries in whole shares, a non-zero `entry_commission`, P&L after fees, a protective stop, and at most 2 active trades. If the daily summary's fee line warns, correct `COMMISSION_PLAN`.
5. [ ] Backtest `trend_pullback` on 5 ETFs over 5 years with the Small account preset, starting at $100, $300 and $1,000, on both fee plans. Record the results below. **Go for live money only if the return after fees is positive at your planned equity and fee plan.** Otherwise keep paper trading or add funds.

| ETFs | Start | Plan | Trades | Win rate | Return after fees | Max drawdown | Fees paid |
|------|-------|------|--------|----------|-------------------|--------------|-----------|
| | | | | | | | |

### 2. Paper soak: 5 trading days in a row

Run the production stack (`scripts/bot.sh up`) on paper with the settings chosen above.

Check each day:
- [ ] `/api/v1/system/health` is ok during market hours, and the summary email arrived.
- [ ] No unexplained entries in `error.log`.
- [ ] Every symbol with an open trade had prices all day (no `PRICE_FEED_STALE`).
- [ ] The reconciler summary shows zero unresolved mismatches.
- [ ] Every signal in `strategy_signals` produced at most one trade.
- [ ] Every open trade has a resting GTC stop in the Gateway/TWS order list.
- [ ] Every closed trade has exit fills and P&L matching IBKR's execution report.
- [ ] The log shows a clean reconnect after IBKR's nightly restart.

Run once during the soak:
- [ ] Restart the backend during market hours with a position open. The monitor and reconciler resume, and the position stays protected.
- [ ] Stop the Gateway for 10 minutes. `BROKER_DISCONNECTED` then `BROKER_RECONNECTED` arrive, and prices resume.
- [ ] Pause: no new entries. Then resume.
- [ ] Manually close one trade, and flatten everything on the last day.
- [ ] Run `scripts/backup.sh` and restore the dump into a scratch database (DEPLOYMENT.md section 6).

### 3. Go live (separate change)

Needs everything above to pass and your explicit go-ahead. It means live credentials, `IBKR_TRADING_MODE=live`, `ENVIRONMENT=production`, Gateway `TRADING_MODE: live`, port 4003, and `TRADING_CAPITAL_LIMIT` unset.

## Optional

- [ ] **Show growth mode on the Home page.** `/trading/status` returns `growth_mode` and `frontend/src/types/api.ts` has its type, but nothing displays it. Show a chip with equity and the $2,000 exit level while `growth_mode.active` is true.
- [ ] **AWS host.** Only if you choose AWS over your Mac or another always-on machine. CDK in Python: default VPC; one t3.small EC2 instance (Amazon Linux 2023, Docker) with no inbound security-group rules; SSM Session Manager for shell and port forwarding (8080, 5900); encrypted gp3 volume with daily snapshots; an IAM role with `AmazonSSMManagedInstanceCore` and write access to one S3 backup bucket; secrets in Secrets Manager, rendered into `.env.prod` at boot.
- [ ] **Persist the trailing stop.** When `_update_trailing_stop` ratchets by at least 0.1R, write `current_stop_price` (never the audit column `stop_loss_price`). `_track` and `_resync` then start from it, so a restart keeps the raised stop.
- [ ] **Strategy cleanup.** Centralize `min_separation_pct` parsing in `filters.py`; make `min_separation_ok` return `False` when the slower value is ≤ 0; make `CompositeStrategy` return HOLD when the combined stop would be `None`; fetch extra-symbol bars with `asyncio.gather`.
- [ ] **Repository tidy-up.** Move `test_ibkr_connection.py` to `backend/scripts/check_gateway.py`. Split `requirements-dev.txt` (pytest, pytest-asyncio, pytest-cov) from the runtime requirements; the Dockerfile installs runtime only, CI installs both.

## Backlog

- **Options (iron condor):** options data feed, multi-leg orders and database models, spread-based risk calculation.
- **ML signals:** feature pipeline, offline training with walk-forward validation, model registry, `MLStrategy`.
- **Mobile PWA:** manifest and service worker, responsive layout, web push (needs HTTPS).
- **Second broker (Alpaca, Tradier):** new `BaseBroker` implementation and mapper, per-broker paper-mode guard.

### Dividend investing Stretegy

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
