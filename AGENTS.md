# AGENTS.md

Steering file for AI coding agents. Fill in every section; placeholders in `<angle brackets>`
are treated as "not set" by the pipeline.

## Build command
```sh
cd frontend && npm run build
```

## Test command
```sh
cd backend && python3 -m pytest tests/unit -q
```

## Lint command
```sh
cd backend && ruff check .
cd frontend && npm run lint
```

## Architecture notes
Two services: `backend/` (FastAPI, Python 3.11) and `frontend/` (React 18 + TypeScript, Vite).

Backend layout under `backend/app/`: `api/v1/` holds FastAPI routes; `core/` holds trading logic —
`core/strategy_engine/` (strategies and registry), `core/execution/` (order manager, position
monitor, reconciler, trade handler), `core/risk/` (risk calculations, circuit breaker, costs,
growth), `core/backtesting/` and `core/jobs/` (bar refresh and daily summary).
`brokers/` wraps broker APIs behind `brokers/base.py`, with `ibkr/` and `mock/` implementations.
`db/models/` contains SQLAlchemy models, migrated via `alembic/versions/`.
`db/repositories/` contains database repositories. `data/` holds the market data feed and cache.
`monitoring/logger.py` defines logging; `notifications/` dispatches alerts.
Entry point is `app/main.py`. Config lives in `app/config.py` and `.env.example`.

Frontend layout under `frontend/src/`: `pages/`, `components/`, `hooks/`, `store/liveStore.ts`,
`api/client.ts`, and `types/api.ts`.

Backend tests live in `backend/tests/unit/`, `backend/tests/integration/`, and
`backend/tests/load/`. CI (`.github/workflows/ci.yml`) runs Ruff and the full pytest suite with
Postgres and Redis services, then runs frontend type checking and linting and builds both Docker
images.

## Conventions
Backend: Ruff configuration is in `backend/ruff.toml` (Python 3.11 target, line length 100,
rules `E, F, I, UP, B, SIM`, first-party import group `app`). Pytest configuration is in
`backend/pytest.ini` (`asyncio_mode = auto`, tests under `tests/`, files named `test_*.py`).

Frontend: ESLint is configured in `frontend/eslint.config.js`; the lint script uses
`--max-warnings 0`. TypeScript is checked with `tsc`.

## Do not touch
- backend/alembic/versions/
- .env.example
- .env.prod.example
- frontend/package-lock.json
- .github/workflows/ci.yml
- docs/*.png
