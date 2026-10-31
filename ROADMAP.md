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
