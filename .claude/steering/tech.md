# tech.md — Technical Stack & Constraints

## Approved Tech Stack

### Backend

| Layer | Technology | Rationale |
|-------|-----------|-----------|
| Language | Python 3.11+ | Financial libs ecosystem, strong async, ML-ready |
| Framework | FastAPI | Async REST + WebSocket, auto docs, type hints |
| Primary DB | PostgreSQL 15+ | Relational trading history, JSONB strategy configs |
| Time-series | TimescaleDB (extension) | Optimized for market data and performance metrics |
| Cache / Pub-Sub | Redis 7+ | Real-time data cache, live dashboard updates |
| Task Queue | Celery (Phase 3+) | Scheduled strategy runs and async notifications |

### Frontend

| Layer | Technology | Rationale |
|-------|-----------|-----------|
| Framework | React 18+ with TypeScript | Type safety, rich charting ecosystem, PWA support |
| Charting | TradingView Lightweight Charts | Technical analysis visualizations |
| UI Components | Tailwind CSS | Responsive design, minimal bundle |
| Data Fetching | React Query (TanStack Query) | Caching, background refresh, WebSocket integration |
| State Management | Zustand or React Context | Lightweight; avoid Redux for personal-scale app |

### Infrastructure & Tooling

| Tool | Purpose |
|------|---------|
| Docker + Docker Compose | Local development environment |
| GitHub Actions | CI/CD pipeline |
| Alembic | PostgreSQL schema migrations |
| SQLAlchemy 2.0 | ORM and query layer |
| Pydantic v2 | Data validation and API schemas |
| Pytest | Unit and integration testing |
| Python `logging` module | Structured application logging |

### Broker Integration

| Broker | Status | Library |
|--------|--------|---------|
| Interactive Brokers | Initial / Active | `ib_insync` or official IBKR API |
| Additional Brokers | Future | Abstracted via broker interface |

---

## Technical Constraints

- **Scale Target**: 5 stocks, 3 concurrent strategies (Phase 1–3); designed to scale
- **Market Data**: Temporary storage with overwrite policy (not a permanent data warehouse)
- **Risk Rule**: 1% max loss per trade is a hard system constraint, not a config option
- **Broker Lock-in**: All broker interactions must go through an abstract interface layer
- **No External Monitoring Services**: Logging and metrics are built-in (no Datadog, Sentry, etc.) in initial phases
- **Historical Data**: Minimum 1-year lookback for backtesting support
- **Log Retention**: 10MB per file, 5 backup files; weekly archival; monthly cleanup

---

## Approved Patterns

- **Async-first**: Use `async/await` throughout FastAPI routes and data handlers
- **Dependency Injection**: FastAPI `Depends()` for DB sessions, broker clients, and services
- **Repository Pattern**: Abstract all database access behind repository classes
- **Strategy Interface**: All strategies implement a common abstract base class
- **Structured Logging**: JSON-formatted logs with contextual fields (trade ID, strategy, timestamp)
- **Environment-based Config**: All secrets and environment-specific settings via `.env` / `pydantic-settings`

## Anti-Patterns (Do Not Use)

- ❌ Synchronous blocking calls inside async route handlers
- ❌ Direct SQL strings — use SQLAlchemy ORM or query builder
- ❌ Hardcoded API keys or secrets anywhere in source code
- ❌ Broker-specific logic outside the broker abstraction layer
- ❌ Storing sensitive financial data in frontend state or localStorage
- ❌ Fat controllers — keep FastAPI routes thin, push logic into service layer

---

## Key Python Libraries

```txt
fastapi
uvicorn[standard]
sqlalchemy[asyncio]
alembic
asyncpg
redis[asyncio]
pydantic-settings
pandas
numpy
scipy
ib_insync          # Interactive Brokers
httpx              # Async HTTP client
celery             # Task scheduling (Phase 3+)
pytest
pytest-asyncio
```

---

> See `product.md` for feature context and business goals.
> See `structure.md` for where each technology's code lives in the project.
