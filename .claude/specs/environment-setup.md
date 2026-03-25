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

# Runtime environment. Controls MockBroker vs IBKRClient selection and
# enables/disables the live trading guard in IBKRClient.connect().
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

# IB Gateway host. Always localhost for local development.
IBKR_HOST=127.0.0.1

# IB Gateway API port. Default: 4001 (Gateway), 7497 (TWS).
IBKR_PORT=4001

# Client ID for this application's connection to Gateway.
# Must be unique per simultaneous connection. Use 1 for the main app.
IBKR_CLIENT_ID=1

# Paper trading credentials — used by the ib-gateway-docker image for
# automated login (cloud deployment). Not read by the FastAPI app directly;
# the app connects to an already-authenticated Gateway via socket.
# Still useful to have defined for the Docker setup later.
IBKR_USERNAME=
IBKR_PASSWORD=

# Trading mode. Must be 'paper' in development — the application enforces
# this via a hard guard in IBKRClient.connect().
# Values: paper | live
IBKR_TRADING_MODE=paper

# --- Notifications --------------------------------------------------------

# SMTP connection string for email notifications (Phase 3+).
# Format: smtp://user:password@host:port
NOTIFICATION_EMAIL_SMTP=

# --- VNC (cloud deployment only, not needed locally) ----------------------

# Password for VNC access to the ib-gateway-docker container.
# Leave empty for local development.
VNC_PASSWORD=
```

---

## Variable Reference

### Application

**`ENVIRONMENT`**
Controls which broker implementation is injected and whether the live trading guard is active. `development` selects `MockBroker` by default and raises an error if `IBKR_TRADING_MODE=live`. `production` selects `IBKRClient`.
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
The host where IB Gateway is listening. Always `127.0.0.1` for local development. In a Docker deployment, this would be the Gateway container's hostname on the internal network.
- Required: yes
- Local default: `127.0.0.1`

**`IBKR_PORT`**
The TCP port Gateway exposes for API connections. `4001` is the IB Gateway default. `7497` is the TWS default — only relevant if you switch to running TWS instead of Gateway.
- Required: yes
- Local default: `4001`

**`IBKR_CLIENT_ID`**
An integer identifying this application's connection to Gateway. Must be unique per simultaneous connection to the same Gateway instance. If you run a separate test script alongside the app, give the script a different ID (e.g. `99`).
- Required: yes
- Local default: `1`

**`IBKR_USERNAME` / `IBKR_PASSWORD`**
Your IBKR paper trading username and password. These are **not read by the FastAPI application directly** — the app connects to an already-authenticated Gateway via socket and never sees your credentials. These variables exist for the `ib-gateway-docker` automated login setup used in cloud deployment. Define them now so the Docker config is ready when you need it.
- Required for cloud deployment; optional locally
- Security note: plaintext in `.env` is a pragmatic tradeoff for local development. Treat this file like a password — don't share it, don't paste it in chat, don't leave it on shared machines.

**`IBKR_TRADING_MODE`**
Controls paper vs live mode. The application reads this and enforces that `development` + `live` cannot be set simultaneously. Must default to `paper` — never change this to `live` during development.
- Required: yes
- Local default: `paper`
- ⚠️ Changing to `live` on a production deployment places real orders with real money.

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
    ibkr_port: int = 4001
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
