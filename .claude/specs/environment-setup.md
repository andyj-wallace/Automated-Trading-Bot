# environment-setup.md — Environment Variables & Local Configuration

> This document is the single source of truth for all environment variables used by the
> application. It covers local setup, the `.env` / `.env.example` pattern, security rules,
> and a complete variable reference.
>
> Referenced by: `tasks.md § Layer 1`, `ibkr-gateway.md`, `design.md § Security Considerations`

---

## The `.env` Pattern

The project uses two files to manage environment configuration:

| File | Committed to git? | Purpose |
|------|:-----------------:|---------|
| `.env.example` | ✅ Yes | Template showing every variable name with safe placeholder values. Documents what's required. |
| `.env` | ❌ **Never** | Your actual local values including real credentials. Never committed. |

When you clone the repo (or set up a new environment), you copy `.env.example` to `.env` and fill in your real values. The application reads from `.env` at runtime via `pydantic-settings`.

---

## Critical: `.gitignore` Must Be Set First

Before adding any credentials to `.env`, confirm `.env` is in `.gitignore`. This must be true before task 1.1 is complete — not after.

Your root `.gitignore` must contain:

```
# Environment variables — never commit real credentials
.env
.env.local
.env.*.local

# Keep the example template
!.env.example
```

The `!.env.example` line explicitly un-ignores the example file so it always gets committed. Without this, it's easy to accidentally gitignore the template too.

To verify nothing sensitive is staged before a commit:

```bash
git status          # .env should never appear here
git diff --cached   # confirm no credential values in staged changes
```

---

## Local Setup Steps

**First time only:**

```bash
# 1. Copy the example template
cp .env.example .env

# 2. Open .env and fill in your values
#    (see Variable Reference below for what each one means)

# 3. Verify .env is ignored
git check-ignore -v .env
# Expected output: .gitignore:2:.env    .env
# If there's no output, .env is NOT ignored — fix .gitignore before proceeding
```

**Each time you add a new variable to the codebase:**
1. Add it to `.env.example` with an empty or safe default value
2. Add it to your local `.env` with the real value
3. Document it in the Variable Reference section below

---

## `.env.example` — Full Template

This is the canonical template. Copy this content verbatim into `.env.example` at the project root. Your `.env` is a copy of this with real values filled in.

```env
# =============================================================================
# environment-setup.md — see that file for full documentation of each variable
# =============================================================================

# --- Application ----------------------------------------------------------

# Runtime environment. Enables/disables the live trading guard in
# IBKRClient.connect(). The broker is chosen by BROKER, not by this.
# Values: development | production
ENVIRONMENT=development

# Log level for all application loggers.
# Values: DEBUG | INFO | WARNING | ERROR | CRITICAL
LOG_LEVEL=INFO

# Secret key used for any internal signing/hashing. Generate with:
#   python -c "import secrets; print(secrets.token_hex(32))"
SECRET_KEY=

# --- Database -------------------------------------------------------------

# Full async PostgreSQL connection string.
# Format: postgresql+asyncpg://user:password@host:port/dbname
DATABASE_URL=postgresql+asyncpg://trading:trading@localhost:5432/trading_bot

# --- Redis ----------------------------------------------------------------

# Full Redis connection string.
REDIS_URL=redis://localhost:6379/0

# --- IBKR / IB Gateway ----------------------------------------------------

# IB Gateway host as seen by the backend: 127.0.0.1 for a backend on the host
# (Docker Gateway or native app); ib-gateway for a backend inside docker-compose.yml.
IBKR_HOST=127.0.0.1

# IB Gateway API port. Native Gateway: 4002 paper / 4001 live.
# ib-gateway-docker image: 4004 paper / 4003 live. TWS: 7497 paper / 7496 live.
IBKR_PORT=4002

# Client ID for this application's connection to Gateway.
# Must be unique per simultaneous connection. Use 1 for the main app.
IBKR_CLIENT_ID=1

# Paper trading credentials — the Docker Gateway (docker compose --profile ibkr)
# uses them to log in by itself. Not read by the FastAPI app directly;
# the app connects to an already-authenticated Gateway via socket.
IBKR_USERNAME=
IBKR_PASSWORD=

# Daily Docker Gateway restart (ET); Sunday's restart asks for the weekly
# IBKR Mobile approval, so pick a time you're awake.
IB_AUTO_RESTART_TIME="07:00 PM"

# Trading mode. Must be 'paper' in development — the application enforces
# this via a hard guard in IBKRClient.connect().
# Values: paper | live
IBKR_TRADING_MODE=paper

# --- Notifications --------------------------------------------------------

# SMTP connection string for email notifications (Phase 3+).
# Format: smtp://user:password@host:port
NOTIFICATION_EMAIL_SMTP=

# --- VNC (Docker Gateway screen) -------------------------------------------

# Password for the Docker Gateway's VNC screen (last-resort manual login).
# Leave empty to keep VNC off.
VNC_PASSWORD=
```

---

## Variable Reference

### Application

**`ENVIRONMENT`**
Controls the live trading guard and the security audit. `development` raises an error if `IBKR_TRADING_MODE=live`. It does **not** choose the broker: `BROKER` does (`mock` = `MockBroker`, `ibkr` = `IBKRClient`).
- Required: yes
- Default in example: `development`

**`LOG_LEVEL`**
Sets the minimum log level across all four log streams (`trading`, `risk`, `system`, `error`). Use `DEBUG` when actively debugging, `INFO` otherwise.
- Required: yes
- Default in example: `INFO`

**`SECRET_KEY`**
Used for any internal signing. Generate a fresh value for each environment — never share between local and production. Generate with: `python -c "import secrets; print(secrets.token_hex(32))"`
- Required: yes
- Default in example: empty — must be set before first run

---

### Database

**`DATABASE_URL`**
Full async PostgreSQL connection string. The `postgresql+asyncpg://` scheme is required for SQLAlchemy async. For local Docker setup (task 1.2), the default value in the example matches the Docker Compose service configuration.
- Required: yes
- Local default: `postgresql+asyncpg://trading:trading@localhost:5432/trading_bot`

---

### Redis

**`REDIS_URL`**
Redis connection string. Database index `/0` is used for all application data. For local Docker setup, the default matches the Docker Compose service.
- Required: yes
- Local default: `redis://localhost:6379/0`

---

### IBKR / IB Gateway

**`IBKR_HOST`**
The host where IB Gateway is listening, as seen by the backend: `127.0.0.1` for a backend on the host (Docker Gateway via `docker compose --profile ibkr`, or the native app), `ib-gateway` for a backend inside a compose stack, `host.docker.internal` for a backend in Docker talking to the native app.
- Required: yes
- Local default: `127.0.0.1`

**`IBKR_PORT`**
The TCP port Gateway exposes for API connections. Native IB Gateway: `4002` paper, `4001` live. The ib-gateway-docker image: `4004` paper, `4003` live. TWS uses `7497` paper / `7496` live. Never point the bot at a live port.
- Required: yes
- Local default: `4002`

**`IBKR_CLIENT_ID`**
An integer identifying this application's connection to Gateway. Must be unique per simultaneous connection to the same Gateway instance. If you run a separate test script alongside the app, give the script a different ID (e.g. `99`).
- Required: yes
- Local default: `1`

**`IBKR_USERNAME` / `IBKR_PASSWORD`**
Your IBKR paper trading username and password. These are **not read by the FastAPI application directly** — the app connects to an already-authenticated Gateway via socket and never sees your credentials. The Docker Gateway (`docker compose --profile ibkr`, and `docker-compose.prod.yml` via `.env.prod`) uses them to log in by itself.
- Required for the Docker Gateway; not needed with the native app
- Security note: plaintext in `.env` is a pragmatic tradeoff for local development. Treat this file like a password — don't share it, don't paste it in chat, don't leave it on shared machines.

**`IB_AUTO_RESTART_TIME`**
Daily restart time (ET) of the Docker Gateway. Sunday's restart asks for the weekly IBKR Mobile approval, so pick a time you're awake; keep it after 16:45 ET and before 23:00 ET (IBKR's nightly reset). The backend reads the same value to tell "daily restart" and "weekly approval" outages apart. Accepts `"07:00 PM"` or `"19:00"`.
- Required: no
- Default: `07:00 PM`

**`IBKR_TRADING_MODE`**
Controls paper vs live mode. The application reads this and enforces that `development` + `live` cannot be set simultaneously. Must default to `paper` — never change this to `live` during development.
- Required: yes
- Local default: `paper`
- ⚠️ Changing to `live` on a production deployment places real orders with real money.

---

**`BROKER`**
Selects the broker implementation: `mock` (simulated, default) or `ibkr` (real IB Gateway connection). `ENVIRONMENT` has no effect on this choice.
- Required: no
- Default: `mock`

### Trading engine

| Variable | Default | Meaning |
|----------|---------|---------|
| `ENGINE_ENABLED` | `true` | Start the trading engine with the API. `false` runs the API only (tests force this). |
| `LOG_DIR` | `logs` | Directory for log files. |
| `CORS_ORIGINS` | `["http://localhost:5173"]` | Allowed CORS origins, as a JSON list. |
| `SCHEDULER_INTERVAL_SECONDS` | `60` | Seconds between strategy evaluation cycles. |
| `STRATEGY_EVAL_START_ET` / `STRATEGY_EVAL_END_ET` | `09:45` / `15:45` | Strategy evaluation window (ET) on NYSE trading days. |
| `BAR_REFRESH_TIME_ET` | `16:30` | Daily bar refresh time (ET). |
| `DAILY_SUMMARY_TIME_ET` | `16:45` | Daily summary time (ET). |
| `PRICE_STALE_SECONDS` | `120` | Max age of a cached price before strategies ignore it. |

### Risk

The 1% per-trade risk rule is fixed in code and is not a setting.

| Variable | Default | Meaning |
|----------|---------|---------|
| `MAX_PORTFOLIO_RISK_PCT` | `0.05` | Max aggregate open risk; must be > 0 and <= 0.10. |
| `MIN_REWARD_TO_RISK` | `2.0` | Minimum reward-to-risk ratio. |
| `DAILY_LOSS_LIMIT_PCT` | `0.03` | Circuit breaker daily loss limit. |
| `MAX_CONSECUTIVE_LOSSES` | `5` | Circuit breaker consecutive-loss limit per strategy. |
| `STALE_TRADE_MAX_DAYS` | `14` | Close trades open longer than this. |

### Execution / broker

| Variable | Default | Meaning |
|----------|---------|---------|
| `ORDER_FILL_TIMEOUT_SECONDS` | `30` | Seconds to wait for an order fill. |
| `BROKER_PROTECTIVE_STOPS` | `true` | Keep a GTC stop order at the broker for each open position. |
| `IBKR_MARKET_DATA_TYPE` | `1` | 1 = live, 3 = delayed (paper accounts only). |

---

### Notifications

**`NOTIFICATION_EMAIL_SMTP`**
SMTP connection string for the email notification channel (implemented in Phase 3, task 16.4). Leave empty until then — the `NotificationDispatcher` should handle a missing value gracefully and log a warning rather than crashing at startup.
- Required: no (Phase 3+)

---

### VNC

**`VNC_PASSWORD`**
Only used by the `ib-gateway-docker` cloud deployment container to protect the VNC server. Not needed locally. Leave empty.
- Required: no (cloud deployment only)

---

## pydantic-settings Configuration

In `app/config.py`, variables are loaded via `pydantic-settings`. Required variables (no default) will cause a startup error with a clear message if missing — this is intentional. Never add a default value for `SECRET_KEY`, `DATABASE_URL`, or `IBKR_USERNAME` in code.

```python
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    # Application
    environment: str
    log_level: str = "INFO"
    secret_key: str  # no default — must be set

    # Database
    database_url: str  # no default — must be set

    # Redis
    redis_url: str = "redis://localhost:6379/0"

    # IBKR
    ibkr_host: str = "127.0.0.1"
    ibkr_port: int = 4002
    ibkr_client_id: int = 1
    ibkr_username: str = ""
    ibkr_password: str = ""
    ibkr_trading_mode: str = "paper"

    # Notifications
    notification_email_smtp: str = ""

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
```

---

## Adding New Variables

As the project grows and new services are added (e.g. SMTP credentials in Phase 3), follow this checklist every time:

- [ ] Add variable to `.env.example` with an empty or safe default, and a comment explaining it
- [ ] Add variable to your local `.env` with the real value
- [ ] Add a field to `Settings` in `app/config.py` — required fields have no default, optional fields do
- [ ] Add an entry to the Variable Reference section in this document
- [ ] If the variable is sensitive (password, key, token), note the security considerations

---

> See `ibkr-gateway.md` for how IBKR credentials are used at runtime.
> See `design.md § Security Considerations` for the live trading guard specification.
> See `tasks.md § Layer 1` for when `.env.example` is created (task 1.4).
