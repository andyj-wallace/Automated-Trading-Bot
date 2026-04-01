# product.md — Product Overview

## Purpose

A personal algorithmic trading bot that executes and manages trading strategies for stocks, with a modular architecture designed for future expansion into options trading. The system emphasizes strict risk management, comprehensive monitoring, and configurable automation levels.

---

## Target Users

**Primary User: Solo Retail Trader (Personal Use)**
- Technically proficient individual managing a personal trading portfolio
- Wants automated strategy execution without full black-box automation
- Requires transparency into risk exposure at all times
- Values backtesting before committing capital to new strategies

---

## Key Features

### Trading Strategy Management
- Support for multiple concurrent strategies with enable/disable controls
- Initial strategies:
  - 50 vs 200 day moving average crossover
  - Stock trend vs 200 day moving average
  - Mean reversion prediction
- Strategy chaining and combination functionality
- Visual parameter configuration interface
- Performance visualization and strategy comparison tools

### Risk Management
- **1% rule**: Maximum 1% of total account balance at risk on any single, individual trade
  - Applies per-trade, not portfolio-wide — multiple concurrent trades each carry their own 1% cap
  - Example: 5 open trades each risking 1% = 5% total capital at risk simultaneously
- **Stop-loss required**: Every trade must have a stop-loss price; the risk engine owns stop-loss calculation — strategies may suggest a value, but the risk engine validates or overrides it
- **Take-profit**: Every trade is assigned a take-profit target; the risk engine validates a strategy-suggested target or calculates one mechanically from the R:R ratio
- **Reward-to-risk ratio**: Configurable minimum R:R ratio (default 2:1); trades that cannot achieve the minimum ratio are rejected
  - Example at 2:1: $5 stop distance → $10 required profit target
  - Take-profit calculated as: `entry + (stop_distance × min_rr_ratio)`
  - A strategy may suggest a take-profit; if it meets the minimum R:R it is accepted; if not the trade is rejected
  - With 2:1, profitability is achievable even winning fewer than 50% of trades
- **Portfolio max risk**: Maximum aggregate open exposure defaults to 5% of account balance; hard system ceiling is 10% — new trades are rejected if they would push total exposure over the active limit
- **Internal position monitoring**: Stop-loss and take-profit levels are tracked internally by a `PositionMonitor` that watches the live price feed; when a level is hit, a close order is submitted automatically — neither level needs to be sent to the broker
- **Optional broker stop-loss**: Each strategy may opt in to sending its stop-loss as a native stop order to the broker (provides protection if the system goes offline); take-profit is always managed internally
- Real-time per-trade and aggregate risk monitoring
- Risk threshold alerts tracking total open exposure across all active trades

### Monitoring & Analytics Dashboard
- Real-time risk exposure relative to 1% threshold (primary focus)
- Active trades with live P&L
- Strategy performance metrics
- System health and alert notifications
- Historical trading and performance analysis

### Backtesting System
- One-year historical data support
- Market condition simulation
- Strategy optimization and performance comparison tools

### Notification System
- Trade execution alerts
- Risk threshold warnings
- System health alerts
- Multi-channel delivery (email and mobile)

---

## Business Goals & Success Metrics

| Goal | Metric |
|------|--------|
| Per-trade risk discipline | Zero individual trades exceeding 1% of account balance; stop-loss required on every trade |
| Portfolio risk discipline | Aggregate open exposure never exceeds configured max (default 5%, hard ceiling 10%) |
| R:R discipline | No trade entered unless minimum reward-to-risk ratio (default 2:1) is achievable |
| System reliability | Uptime during market hours ≥ 99% |
| Strategy visibility | All active strategies visible with real-time P&L |
| Backtesting coverage | Minimum 1 year historical data per strategy |
| Notification delivery | Alerts delivered within 60 seconds of trigger event |

---

## Competitive Differentiators (vs. Off-the-Shelf Tools)

- **Per-trade risk enforcement** — hard 1% rule applies to each individual trade with mandatory stop-loss; not a portfolio-level cap
- **Portfolio risk ceiling** — aggregate exposure capped at 5% by default (10% hard limit), enforced before each new trade
- **R:R filter** — trades that cannot deliver the configured minimum reward-to-risk ratio are rejected before execution
- **Internal position management** — stop and target levels tracked by the system, not delegated to the broker; no reliance on broker-side stop orders (though optional per strategy)
- **Modular strategy engine** — add/remove/chain strategies without system changes
- **Full observability** — every trade, risk check, and system event is logged
- **Personal-scale design** — optimized for 5 stocks and 3 strategies, not enterprise complexity
- **Broker abstraction layer** — swap or add brokers (starting with Interactive Brokers) without rewriting strategy logic

---

## Future Expansion

- Options trading (iron condor, bull/bear prediction, intra-week mean reversion)
- Mobile application (PWA-first, then native)
- Machine learning integration for signal generation
- Additional broker integrations
- High-frequency trading capabilities

---

> See `tech.md` for approved stack and infrastructure details.
> See `structure.md` for project layout and naming conventions.
