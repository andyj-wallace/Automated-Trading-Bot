# Deployment and Operations Runbook

Paper trading only. Never set `IBKR_TRADING_MODE=live`, never use Gateway ports 4001/4003.

The stack (`docker-compose.prod.yml`): `ib-gateway` (paper), `db` (TimescaleDB), `redis`, `backend` (one process, one worker), `frontend` (nginx). Only `127.0.0.1:8080` (UI/API) and `127.0.0.1:5900` (VNC) are published. Run it on an always-on host and reach it by SSH tunnel or Tailscale.

The `ib-gateway` container logs in by itself (IBC, inside the image, types the paper username and password) and restarts every day at `IB_AUTO_RESTART_TIME`. You never open the Gateway app. The one manual step is approving a login in IBKR Mobile about once a week (section 2).

`scripts/bot.sh` wraps the compose commands: `up`, `down`, `status`, `logs [service]`, `restart-gateway`.

## 1. First boot

1. Create the secrets file (never commit it):
   ```bash
   cp .env.prod.example .env.prod
   # fill in SECRET_KEY, POSTGRES_PASSWORD (URL-safe characters only), IBKR_USERNAME and IBKR_PASSWORD
   # (the paper account's own login), VNC_PASSWORD, IB_AUTO_RESTART_TIME, notification settings
   chmod 600 .env.prod
   ```
2. Create the UI password file (never commit it):
   ```bash
   mkdir -p deploy/nginx
   docker run --rm httpd:2.4-alpine htpasswd -nbB <user> <password> > deploy/nginx/.htpasswd
   ```
3. Quit the native IB Gateway app if it is running (IBKR allows one session per login).
4. Start: `scripts/bot.sh up`. Migrations run automatically when the backend starts.
5. Within a minute IBKR Mobile asks you to approve the login. Tap Approve.
6. `scripts/bot.sh status` should show `Broker: connected`. Then check health (section 5).
7. Open the UI through a tunnel: `ssh -L 8080:localhost:8080 <host>` then http://localhost:8080 (basic-auth user/password from step 2).

If the paper account has no US-equities data subscription (IB errors 354, 10089, 10167), set `IBKR_MARKET_DATA_TYPE=3` (delayed) in `.env.prod`. Paper only.

## 2. The weekly IBKR Mobile approval

- The daily restart at `IB_AUTO_RESTART_TIME` (default 7:00 PM ET) needs no approval.
- Once a week IBKR wants a full login: the first one after Sunday 01:00 ET, which is Sunday's restart. IBKR Mobile asks you to approve it; tap Approve. Pick a restart time when you're awake on Sundays, after 16:45 ET and before 23:00 ET (IBKR's nightly reset).
- Missed it? The Gateway retries and sends a new request (`TWOFA_TIMEOUT_ACTION: restart`). After 5 minutes down you get a `BROKER_DISCONNECTED` email/SMS that says what to do, and the dashboard shows a red banner with the same steps.
- No request arriving? `scripts/bot.sh restart-gateway`, then approve.
- Last resort, VNC: `ssh -L 5900:localhost:5900 <host>`, connect a VNC client to `localhost:5900` with `VNC_PASSWORD`, and complete the login in the Gateway window.

While the Gateway is logged out the bot cannot trade and the internal stop monitor sees no prices. With `BROKER_PROTECTIVE_STOPS=true` (the default) every open trade keeps a stop order at IBKR meanwhile.

## 3. Running on a Mac

- Quit the native Gateway app, and remove it from Login Items if it starts by itself.
- Docker Desktop → Settings → General → "Start Docker Desktop when you sign in to your computer". The containers then come back by themselves after a reboot.
- MacBook: keep it plugged in with the lid open (a closed lid sleeps a MacBook unless an external display is attached). System Settings → Battery → Options → "Prevent automatic sleeping on power adapter when the display is off". A sleeping Mac means no trading and no stop monitoring (the stops at IBKR still work).
- Stay logged in to macOS: Docker Desktop is a user app and stops when you log out.
- Moving to an always-on Linux server later: install Docker Engine, copy `.env.prod` and `deploy/nginx/.htpasswd`, run `scripts/bot.sh up`. Nothing else changes.

## 4. Operating the bot

All endpoints are under `/api/v1` (through nginx: `http://localhost:8080/api/v1/...`, basic auth `-u user:password`).

- Status: `scripts/bot.sh status`, or `GET /trading/status` (paused or not, engine state, broker connection with a "what to do" hint).
- Pause automated entries (open positions keep being monitored): `POST /trading/pause`; resume: `POST /trading/resume`. Pause state survives restarts.
- Manual close of one trade: `POST /trades/{id}/close` (or the Close button in the UI). 200 closed, 202 exit pending, 409 not open or market closed, 502 broker failure.
- Flatten everything: `POST /trading/flatten` with body `{"confirm": "FLATTEN"}`. It pauses trading, then closes every open trade. Resume manually afterwards.
- Circuit breaker: `GET /risk/circuit-breaker`; reset only after you understand why it tripped.

Strategies act only on NYSE trading days, 09:45-15:45 ET. Stops, targets and manual closes act only while the market is open; an overnight gap fills at the next open.

## 5. Daily checks

- The daily summary email (16:45 ET) arrived.
- `curl -s -u user:password http://localhost:8080/api/v1/system/health` shows every component ok (database, redis, broker, engine).
- No unresolved reconciler mismatches or unexplained alerts (`docker compose -f docker-compose.prod.yml logs --since 24h backend | grep -i error`; also `logs/error.log` in the `app_logs` volume).
- The backup ran (section 6).

## 6. Backup and restore

Backup (keeps 14 days of database dumps and audit-log copies in `backups/`). Add to host cron for 17:30 ET on weekdays (adjust for the host timezone):
```
30 17 * * 1-5 /path/to/repo/scripts/backup.sh >> /path/to/repo/backups/backup.log 2>&1
```
Copy `backups/` off the host regularly. `logs/audit.log` is append-only; never edit it.

Restore into a scratch database first to verify a dump:
```bash
DC="docker compose -f docker-compose.prod.yml"
$DC exec -T db createdb -U trading trading_bot_restore
$DC exec -T db psql -U trading -d trading_bot_restore -c "CREATE EXTENSION IF NOT EXISTS timescaledb;"
$DC exec -T db psql -U trading -d trading_bot_restore -c "SELECT timescaledb_pre_restore();"
$DC exec -T db pg_restore -U trading -d trading_bot_restore --no-owner < backups/trading_bot_<STAMP>.dump
$DC exec -T db psql -U trading -d trading_bot_restore -c "SELECT timescaledb_post_restore();"
$DC exec -T db psql -U trading -d trading_bot_restore -c "SELECT count(*) FROM trades;"
$DC exec -T db dropdb -U trading trading_bot_restore
```
TimescaleDB requires `timescaledb_pre_restore()` before `pg_restore` and `timescaledb_post_restore()` after. A restore into the live database is the same sequence into a freshly created empty `trading_bot` with the backend stopped (`$DC stop backend`), followed by `$DC start backend`. A few "already exists" warnings from `pg_restore` about the extension are normal.

## 7. Upgrading

```bash
git pull
docker compose -f docker-compose.prod.yml --env-file .env.prod up -d --build
```
Migrations run on backend start. Take a backup first (`scripts/backup.sh`). Prefer upgrading outside market hours; the bot reconciles open positions with the broker on startup.

The Gateway image is pinned (`ghcr.io/gnzsnz/ib-gateway:10.50.1f`). IBC, the auto-login tool inside it, was retired upstream in September 2026 and its replacement is not decided yet (gnzsnz/ib-gateway-docker issue #366). Read the image's release notes before bumping the tag, and bump it outside market hours.

## 8. Rollback

1. `docker compose -f docker-compose.prod.yml stop backend frontend`
2. `git checkout <previous-commit>` and rebuild: `docker compose -f docker-compose.prod.yml --env-file .env.prod up -d --build`
3. If the new version ran a migration that the old code cannot read, restore the pre-upgrade dump (section 6) into the live database before step 2's start, or run `alembic downgrade -1` inside the backend container (`docker compose -f docker-compose.prod.yml exec backend alembic downgrade -1`).

## 9. Troubleshooting

- Backend unhealthy: `docker compose -f docker-compose.prod.yml logs backend`. Look for the broker connection, migration errors, or the single-instance lock (another instance running).
- Broker `reconnecting` or `down` (dashboard banner, `scripts/bot.sh status`): follow the "What to do" line; usually the weekly approval (section 2).
- Broker `account_mismatch`: the Gateway is logged into a non-paper account. Put the paper account's username in `IBKR_USERNAME`.
- `Connection refused` to the Gateway: it is still logging in or waiting for the approval (section 2).
- Auto-login stops working (for example after a Gateway image bump; IBC is retired):
  1. Log in by hand over VNC (section 2).
  2. Run the native Gateway instead. Install it for the host OS from IBKR's **stable** channel (the `ibgateway-latest-*` installers in the repo folder are the "latest" channel and are gitignored). Log in, set the API socket port to `4002`, uncheck "Read-Only API" and "Allow connections from localhost only", and add `host.docker.internal` and `127.0.0.1` to Trusted IPs. In `docker-compose.prod.yml`, delete the `ib-gateway` service, set `IBKR_HOST: host.docker.internal` and `IBKR_PORT: "4002"` on `backend`, and drop `ib-gateway` from its `depends_on`.
- UI returns 401: wrong basic-auth credentials, or the `.htpasswd` file is missing (Docker would then create a directory in its place; delete it and recreate the file).
