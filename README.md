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

---

## Backlog

Major features deferred due to infrastructure scope. Each requires significant new subsystems beyond what is currently in place.

---

### Options Trading — Iron Condor Strategy

Selling an iron condor requires four simultaneous legs (two puts, two calls at different strikes), which goes well beyond the current single-leg equity order model.

**What needs to be built:**
- **Options data feed** — IBKR provides options chains via `reqSecDefOptParams` and live Greeks via `reqMktData` with tick types 10–13. `MarketDataFeed` needs a parallel options feed that subscribes to chain snapshots and streams IV, delta, theta, gamma per contract.
- **ORM / DB models** — A new `options_contracts` table (underlying, expiry, strike, right, multiplier) and an `options_positions` table tracking the four legs of each condor as a unit. The `trades` table status lifecycle does not cleanly map to multi-leg positions and would need extension or a parallel model.
- **Multi-leg `BaseBroker` interface** — `place_order()` currently sends a single `OrderRequest`. Options require a `ComboOrder` or `BagOrder` that groups legs with their own actions and ratios. `IBKRClient` wraps this via `ib_async`'s `Contract` with `comboLegs`.
- **Risk model extension** — The 1% rule applies to max loss on the spread, not a simple `entry − stop` calculation. Max loss on an iron condor is `(spread width − net credit) × 100 × contracts`. `RiskCalculator` needs a new path for multi-leg risk.
- **Strategy implementation** — Entry logic: sell condor when IV rank is elevated (IVR > ~50), wings at 1 SD. Exit logic: close at 50% of max profit, or roll/close if short strikes are tested. Needs `HistoricalDataFetcher` extended to pull historical IV for rank calculation.
- **Frontend panels** — The `ActiveTradesTable` and `Portfolio` page assume a single entry/exit price per trade; a condor position panel needs to show four strikes, net credit, current value, and days-to-expiry.

**Dependencies:** `BaseBroker` options interface, new DB models, `RiskCalculator` multi-leg path, IV data feed.

---

### Machine Learning Signal Integration

Replacing or augmenting hand-coded indicator logic with a trained model that generates `BUY`/`SELL`/`HOLD` signals.

**What needs to be built:**
- **Feature engineering pipeline** — Transform raw OHLCV bars into ML features (returns, rolling vol, RSI, MACD, volume z-scores, etc.). This is a batch job that runs nightly and writes to a `ml_features` TimescaleDB hypertable, keyed on `(symbol, time)`.
- **Training infrastructure** — A `scripts/train_model.py` script (run offline, outside the API) that reads features, trains a classifier (e.g. LightGBM or scikit-learn RandomForest), evaluates on a held-out period, and serialises the model to disk (e.g. `models/{symbol}_{date}.pkl`). Walk-forward validation is required to prevent look-ahead bias.
- **Model registry** — A lightweight `ModelRegistry` that loads the correct model for a given symbol and version at runtime. Models are versioned by training date. A new `ml_models` DB table tracks which model version is active per symbol.
- **`MLStrategy` implementation** — Implements `BaseStrategy`; loads the active model from the registry, constructs the feature vector for the current bar, calls `model.predict_proba()`, and emits a signal above a configurable confidence threshold. Stop-loss is still required — the model provides direction, the risk engine provides sizing and stops.
- **Retraining automation** — A scheduled task (cron or the existing `CronCreate` scheduler) that retrains nightly on the latest data and promotes the new model only if it beats the current model on a validation window.
- **Monitoring** — Track live prediction accuracy (predicted direction vs. actual next-bar return) in TimescaleDB. Alert when accuracy degrades below baseline.

**Dependencies:** Feature pipeline, model training script, model registry, `MLStrategy`, monitoring hooks.

---

### Mobile PWA Frontend

Making the React dashboard installable on iOS/Android and operable on a phone screen.

**What needs to be built:**
- **PWA plumbing** — `manifest.json` with app name, icons, `display: standalone`. A service worker (via Vite PWA plugin: `vite-plugin-pwa`) that precaches the app shell and API responses with a stale-while-revalidate strategy. This is the low-effort part.
- **Mobile layout** — The current layout uses a persistent sidebar and multi-column panels designed for a 1280px+ desktop. On mobile this needs: a bottom tab bar instead of sidebar, single-column panel stack, collapsible sections. All existing panel components would need responsive variants.
- **Touch interactions** — The `ActiveTradesTable` and `WatchlistPanel` are dense tables that are unusable on a 390px screen without tap targets, horizontal scroll handling, and drill-down navigation (tap row → detail sheet).
- **Native push notifications** — The Web Push API requires a VAPID key pair, a push subscription stored per device in the DB, and a server-side push sender in the `NotificationDispatcher`. This replaces the need for Twilio SMS on devices where the PWA is installed, but requires HTTPS in production (service workers are HTTPS-only).
- **Offline behaviour** — The dashboard's real-time data (WebSocket prices, open trades) cannot work offline. The offline experience needs to gracefully degrade: show last-known data from the service worker cache with a clear "offline" indicator rather than broken UI.

**Dependencies:** HTTPS (service workers require it), VAPID key management, push subscription DB table, mobile layout redesign across all pages.

---

### Additional Broker Integration

Adding a second broker (e.g. Alpaca, Tradier) alongside the existing IBKR implementation.

**What needs to be built:**
- **New broker client** — A new `app/brokers/{broker}/client.py` implementing `BaseBroker`. The abstraction layer is already in place; this is mostly mapping the target broker's REST/WebSocket API to the existing interface (`connect`, `get_account_summary`, `get_positions`, `subscribe_price_feed`, `place_order`, `cancel_order`).
- **Broker-specific mapper** — Same pattern as `app/brokers/ibkr/mapper.py` — converts the broker's native response objects to internal `AccountSummary`, `Position`, `OrderRequest`, `PriceBar` models.
- **Auth and config** — New env vars for the broker's API key / OAuth tokens. A second set of fields in `Settings` behind a guard so the existing IBKR config is not disturbed.
- **Broker selection** — `get_broker()` in `app/dependencies.py` currently switches on `BROKER=mock|ibkr`. This becomes `BROKER=mock|ibkr|alpaca|...`. Multi-broker routing (running two brokers simultaneously for different strategies) is a further step that requires a `BrokerRouter` layer.
- **Paper trading parity** — Each broker has its own paper trading environment. The live trading guard in `IBKRClient` (`ENVIRONMENT=development` + `IBKR_TRADING_MODE=live` → reject) needs a parallel check per broker.
- **Data normalisation** — Brokers differ on tick types, bar sizes, and timestamp timezones. The mapper must normalise everything to UTC and the internal `PriceBar` schema before any strategy or risk code sees it.

**Dependencies:** New `BaseBroker` implementation, broker-specific config/auth, `get_broker()` extension, paper-mode guard per broker.
