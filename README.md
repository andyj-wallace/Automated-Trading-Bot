# Automated Trading Bot

Personal algorithmic trading bot for stocks (future: options).

## Stack
- **Backend**: Python / FastAPI / PostgreSQL + TimescaleDB / Redis
- **Frontend**: React + TypeScript / Tailwind CSS
- **Broker**: Interactive Brokers (`ib_insync`)

## Quick Start

```bash
cp .env.example .env
docker compose up -d
cd backend && pip install -r requirements.txt
uvicorn app.main:app --reload
```

## Project Docs
- [Master Plan](trading-bot-masterplan.md)
- [Architecture](`.claude/specs/design.md`)
- [Task Tracker](`.claude/specs/tasks.md`)
