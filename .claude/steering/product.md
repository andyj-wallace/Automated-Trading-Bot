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
- **Stop-loss required**: Every trade must define a stop-loss price before entry; position size is calculated from the distance between entry price and stop-loss to ensure the loss never exceeds 1% of account balance
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
| Risk discipline | Zero individual trades exceeding 1% of account balance; stop-loss required on every trade |
| System reliability | Uptime during market hours ≥ 99% |
| Strategy visibility | All active strategies visible with real-time P&L |
| Backtesting coverage | Minimum 1 year historical data per strategy |
| Notification delivery | Alerts delivered within 60 seconds of trigger event |

---

## Competitive Differentiators (vs. Off-the-Shelf Tools)

- **Per-trade risk enforcement** — hard 1% rule applies to each individual trade with mandatory stop-loss; not a portfolio-level cap
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
