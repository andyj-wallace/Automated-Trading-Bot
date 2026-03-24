# structure.md — Project Structure & Conventions

## Directory Layout

```
trading-bot/
├── .claude/
│   ├── steering/
│   │   ├── product.md
│   │   ├── tech.md
│   │   └── structure.md
│   └── specs/
│       ├── design.md
│       ├── requirements.md
│       └── tasks.md
│
├── backend/
│   ├── app/
│   │   ├── main.py                  # FastAPI app entry point
│   │   ├── config.py                # Settings via pydantic-settings
│   │   ├── dependencies.py          # Shared FastAPI Depends() providers
│   │   │
│   │   ├── api/                     # Route handlers (thin controllers)
│   │   │   ├── v1/
│   │   │   │   ├── trades.py
│   │   │   │   ├── strategies.py
│   │   │   │   ├── portfolio.py
│   │   │   │   ├── risk.py
│   │   │   │   ├── backtesting.py
│   │   │   │   └── system.py
│   │   │   └── websocket.py         # WebSocket endpoint handlers
│   │   │
│   │   ├── core/                    # Business logic and domain services
│   │   │   ├── strategy_engine/
│   │   │   │   ├── base.py          # Abstract strategy base class
│   │   │   │   ├── moving_average.py
│   │   │   │   ├── mean_reversion.py
│   │   │   │   └── registry.py      # Strategy registration and lookup
│   │   │   ├── risk/
│   │   │   │   ├── manager.py       # Risk rule enforcement (1% rule)
│   │   │   │   ├── calculator.py    # Position sizing calculations
│   │   │   │   └── monitor.py       # Real-time threshold monitoring
│   │   │   ├── execution/
│   │   │   │   ├── order_manager.py
│   │   │   │   └── trade_handler.py
│   │   │   └── backtesting/
│   │   │       ├── engine.py
│   │   │       └── simulator.py
│   │   │
│   │   ├── brokers/                 # Broker abstraction layer
│   │   │   ├── base.py              # Abstract broker interface
│   │   │   ├── ibkr/
│   │   │   │   ├── client.py
│   │   │   │   └── mapper.py        # IBKR response → internal model
│   │   │   └── mock/
│   │   │       └── client.py        # Paper trading / test broker
│   │   │
│   │   ├── data/                    # Market data handling
│   │   │   ├── feed.py              # Real-time data ingestion
│   │   │   ├── historical.py        # Historical data fetcher
│   │   │   └── cache.py             # Redis data access layer
│   │   │
│   │   ├── db/                      # Database layer
│   │   │   ├── session.py           # Async SQLAlchemy session factory
│   │   │   ├── models/              # SQLAlchemy ORM models
│   │   │   │   ├── trade.py
│   │   │   │   ├── strategy.py
│   │   │   │   ├── portfolio.py
│   │   │   │   └── system_log.py
│   │   │   └── repositories/        # DB access abstraction
│   │   │       ├── trade_repo.py
│   │   │       ├── strategy_repo.py
│   │   │       └── portfolio_repo.py
│   │   │
│   │   ├── notifications/
│   │   │   ├── dispatcher.py        # Multi-channel delivery logic
│   │   │   ├── email.py
│   │   │   └── mobile.py
│   │   │
│   │   └── monitoring/
│   │       ├── logger.py            # Structured logging setup
│   │       ├── metrics.py           # Performance metrics collection
│   │       └── alerts.py            # Alert rule evaluation
│   │
│   ├── alembic/                     # DB migrations
│   │   ├── versions/
│   │   └── env.py
│   │
│   ├── tests/
│   │   ├── unit/
│   │   ├── integration/
│   │   └── conftest.py
│   │
│   ├── Dockerfile
│   └── requirements.txt
│
├── frontend/
│   ├── src/
│   │   ├── App.tsx
│   │   ├── main.tsx
│   │   │
│   │   ├── pages/
│   │   │   ├── Dashboard.tsx        # Main monitoring view (risk, trades, watchlist panel)
│   │   │   ├── Watchlist.tsx        # Add/remove symbols, manage strategy assignments
│   │   │   ├── SymbolDetail.tsx     # Price chart, signals, trade history for one symbol
│   │   │   ├── Strategies.tsx       # Strategy config, enable/disable, symbol assignment
│   │   │   ├── Portfolio.tsx        # Positions and P&L
│   │   │   ├── Backtesting.tsx
│   │   │   └── SystemHealth.tsx
│   │   │
│   │   ├── components/
│   │   │   ├── watchlist/
│   │   │   │   ├── WatchlistPanel.tsx   # Dashboard panel (price, change, status per symbol)
│   │   │   │   └── SymbolRow.tsx
│   │   │   ├── risk/
│   │   │   ├── trades/
│   │   │   ├── charts/
│   │   │   └── shared/
│   │   │
│   │   ├── hooks/                   # Custom React hooks
│   │   ├── api/                     # API client functions
│   │   ├── store/                   # Zustand state stores
│   │   └── types/                   # Shared TypeScript types
│   │
│   ├── Dockerfile
│   └── package.json
│
├── docker-compose.yml
├── docker-compose.dev.yml
├── .env.example
└── README.md
```

---

## Naming Conventions

### Python (Backend)
| Element | Convention | Example |
|---------|-----------|---------|
| Files | `snake_case.py` | `trade_handler.py` |
| Classes | `PascalCase` | `RiskManager` |
| Functions/methods | `snake_case` | `calculate_position_size()` |
| Constants | `UPPER_SNAKE_CASE` | `MAX_RISK_PERCENT` |
| Async functions | prefix none, use `async def` | `async def get_trade()` |
| Pydantic schemas | `PascalCase` + noun | `TradeCreateRequest`, `TradeResponse` |

### TypeScript (Frontend)
| Element | Convention | Example |
|---------|-----------|---------|
| Files (components) | `PascalCase.tsx` | `RiskPanel.tsx` |
| Files (hooks/utils) | `camelCase.ts` | `useTradeData.ts` |
| Components | `PascalCase` | `ActiveTradesTable` |
| Hooks | `use` prefix | `useRiskMetrics` |
| API functions | `camelCase` verb-noun | `fetchActiveTrades()` |
| Types/Interfaces | `PascalCase` | `TradePosition`, `RiskAlert` |

### API Routes
```
GET    /api/v1/trades              # List trades
POST   /api/v1/trades              # Create trade
GET    /api/v1/trades/{id}         # Get specific trade
GET    /api/v1/symbols             # List watchlist symbols
POST   /api/v1/symbols             # Add symbol to watchlist
DELETE /api/v1/symbols/{ticker}    # Remove symbol from watchlist
GET    /api/v1/strategies          # List strategies
PATCH  /api/v1/strategies/{id}     # Update strategy config (incl. symbol assignments)
GET    /api/v1/portfolio/risk      # Current risk metrics
POST   /api/v1/backtesting/run     # Start backtest
WS     /ws/dashboard               # Real-time dashboard feed (trades, risk, prices)
```

### Database Tables
| Convention | Example |
|-----------|---------|
| `snake_case`, plural | `trades`, `trading_strategies` |
| Junction tables | `strategy_trade_links` |
| Timestamp columns | `created_at`, `updated_at`, `executed_at` |
| Soft deletes | `deleted_at` (nullable timestamp) |

### Environment Variables
```
# Format: COMPONENT_SETTING
DATABASE_URL=
REDIS_URL=
IBKR_HOST=
IBKR_PORT=
IBKR_CLIENT_ID=
NOTIFICATION_EMAIL_SMTP=
SECRET_KEY=
ENVIRONMENT=development|production
```

---

## Architecture Patterns

### Backend Layer Responsibilities

```
API Route (api/)
  └── calls → Service / Core Logic (core/)
                └── calls → Repository (db/repositories/)
                              └── calls → Database
                └── calls → Broker Interface (brokers/)
                └── calls → Cache (data/cache.py)
```

- **Routes** handle HTTP concerns only (request parsing, response formatting)
- **Core** contains all business logic; never imports from `api/`
- **Repositories** are the only layer that touches SQLAlchemy models directly
- **Brokers** are injected via dependency injection, never instantiated in business logic

### Strategy Pattern
All strategies must extend `core/strategy_engine/base.py`:
```python
class BaseStrategy(ABC):
    @abstractmethod
    async def generate_signal(self, market_data: MarketData) -> Signal: ...

    @abstractmethod
    async def calculate_position_size(self, risk_params: RiskParams) -> Decimal: ...

    @abstractmethod
    def get_config_schema(self) -> dict: ...
```

### Log File Placement
```
logs/
├── trading.log          # Trade executions, order status, signals
├── risk.log             # Risk checks, threshold events
├── system.log           # API connections, DB ops, health
└── error.log            # All ERROR and CRITICAL level events
```
- Rotation: 10MB max per file, 5 backups retained
- Weekly archival, monthly cleanup

---

> See `tech.md` for approved libraries and tools used in each layer.
> See `design.md` for data flow and component interaction details.
