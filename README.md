# Automated Trading Bot

Personal algorithmic trading bot for US equities, built on Interactive Brokers.

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Backend | Python 3.11 + FastAPI |
| Database | PostgreSQL 15 + TimescaleDB |
| Cache / Pub-Sub | Redis 7 |
| Broker | Interactive Brokers (via `ib_async`) |
| Frontend | React 18 + TypeScript + Tailwind CSS |
| Infrastructure | Docker Compose |

## Quick Start

### Prerequisites
- Docker and Docker Compose
- Node.js 18+
- Python 3.11+

### 1. Configure environment

```bash
cp .env.example .env
# Edit .env and fill in your values (see .claude/specs/environment-setup.md)
```

### 2. Start services

```bash
docker-compose up -d
```

### 3. Run migrations

```bash
cd backend
pip install -r requirements.txt
alembic upgrade head
```

### 4. Start the API

```bash
cd backend
uvicorn app.main:app --reload
```

API available at http://localhost:8000
Docs at http://localhost:8000/docs

### 5. Start the frontend

```bash
cd frontend
npm install
npm run dev
```

Frontend available at http://localhost:5173

## Project Structure

```
├── backend/          # FastAPI application
│   ├── app/
│   │   ├── main.py
│   │   ├── config.py
│   │   ├── api/        # Route handlers
│   │   ├── core/       # Business logic
│   │   ├── brokers/    # Broker abstraction
│   │   ├── data/       # Market data
│   │   ├── db/         # Models and repositories
│   │   └── monitoring/ # Logging and metrics
│   ├── alembic/        # DB migrations
│   └── tests/
├── frontend/         # React application
│   └── src/
├── docker-compose.yml
└── .env.example
```

## IB Gateway Setup

The backend connects to a locally running IB Gateway via TCP. When running inside Docker the backend reaches the host Mac via `host.docker.internal`.

**Required Gateway API settings** (*Configure → Settings → API → Settings*):
- Socket port: `4002` (paper) / `4001` (live)
- **Uncheck** "Allow connections from localhost only" — Docker containers connect from the bridge network IP, not `127.0.0.1`, so this option blocks them even when the host is in Trusted IPs
- Trusted IPs: `host.docker.internal`, `127.0.0.1`

**`.env` must have:**
```
BROKER=ibkr
IBKR_HOST=host.docker.internal
IBKR_PORT=4002
IBKR_TRADING_MODE=paper
```

**Verify connection after starting the backend:**
```bash
curl -s http://localhost:8000/api/v1/system/health | python3 -m json.tool
# broker.status should be "ok"
```

## Risk Model

Every trade is hard-blocked if it would risk more than **1% of account balance**.
A stop-loss price is mandatory on every order — no stop-loss means automatic rejection.

## Documentation

See `.claude/specs/` for detailed architecture, requirements, and task tracking.
