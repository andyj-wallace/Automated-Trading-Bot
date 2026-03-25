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

## Risk Model

Every trade is hard-blocked if it would risk more than **1% of account balance**.
A stop-loss price is mandatory on every order — no stop-loss means automatic rejection.

## Documentation

See `.claude/specs/` for detailed architecture, requirements, and task tracking.
