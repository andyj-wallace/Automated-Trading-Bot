# Plan: Make IBKR API access hands-off (Gateway automation)

## Context

To trade, the bot needs IB Gateway (IBKR's Java desktop app) running and logged in. Today the user opens the
native Gateway app on their Mac, logs in, approves the login on their phone, and has to notice when it drops
(IBKR's nightly reset, a forced weekly re-login, Mac sleep or reboot). The bot only sees "connection refused":
it retries forever and sends one `BROKER_DISCONNECTED` email/SMS after 5 minutes with no hint about what to do.
The dashboard shows a red "Broker disconnected" chip; the alerts feed only shows alerts that arrived while the
page was open.

Decided with the user:
- **Automate the Gateway, keep IBKR** (no second broker for now). Run the Gateway in Docker
  (`ghcr.io/gnzsnz/ib-gateway`). The IBC tool inside it types the paper username/password and restarts the
  Gateway daily without a second login step.
- **2FA (the second login step) = IBKR Mobile push.** IBKR forces a full re-login about once a week (the first
  login after Sunday 01:00 ET). No tool can remove that. With push, the weekly step is "tap Approve on the phone".
- **Host undecided:** set up the user's Mac first. It is an **Intel MacBook Pro (x86_64, i7-9750H)** running
  Docker Desktop, so it uses the image's amd64 build. Nothing Mac-only goes in the code; the same stack moves
  to an always-on Linux VM (amd64 or arm64) or a Windows machine with Docker Desktop later.
- **Native Gateway installers = fallback only.** On 2026-10-20 the user downloaded the standalone installers
  for every OS into the repo root (`ibgateway-latest-standalone-{macosx-x64.dmg,windows-x64.exe,linux-x64.sh,
  linux-arm.sh}`, about 1.2 GB). The Docker image ships its own Gateway, so none are needed day to day. They
  are **untracked and not gitignored** (only `IB Gateway*/` is): `git add -A` would stage them, and GitHub
  rejects files over 100 MB. Ignore them; never delete or move the user's files.

Outcome: the user never opens the Gateway app. The weekly approval arrives at a sensible hour (Sunday evening,
not midnight). When the Gateway needs them, the email and the dashboard say what is wrong and what to do.

Facts verified on 2026-10-19 (re-check before relying on them):
- `ghcr.io/gnzsnz/ib-gateway:10.50.1f` exists for amd64 and arm64 (Apple Silicon); it is the current `stable`
  (IB Gateway 10.50.1f + IBC 3.24.2). Image env vars used here: `TWS_USERID`, `TWS_PASSWORD`, `TRADING_MODE`,
  `READ_ONLY_API`, `TWOFA_TIMEOUT_ACTION`, `RELOGIN_AFTER_TWOFA_TIMEOUT`, `AUTO_RESTART_TIME` (format
  `hh:mm AM/PM`), `TIME_ZONE` (default `Etc/UTC`, so it must be set), `VNC_SERVER_PASSWORD`.
  `EXISTING_SESSION_DETECTED_ACTION` defaults to `primary`; `TWOFA_DEVICE` unset = the default device.
- Inside the container the paper API listens on 4002 and socat relays it to 4004. Other containers use
  `ib-gateway:4004`; apps on the host use a published `127.0.0.1:4002:4004`.
- **IBC was retired upstream on 2026-09-01.** The image still ships IBC 3.24.2 and works, but gnzsnz has not
  chosen a replacement (gnzsnz/ib-gateway-docker#366; the community drop-in `ibg-controller` keeps the same env
  vars). Pin the exact tag; document what to do if auto-login breaks.
- The backend already reconnects forever (`IBKRClient._supervise`, backoff 5→60 s) and reconciles after every
  reconnect, so a Gateway restart needs no backend change to recover.

## Guardrails for the implementer

- Paper only: never `TRADING_MODE: live`, never ports 4001/4003, never `IBKR_TRADING_MODE=live`.
- Never open, print or copy `.env`, `backend/.env`, `.env.prod`, `credentials.txt`. Use the `*.example` files.
- No new backend or frontend dependencies. Keep `BaseBroker.is_connected()` exactly as it is (11 callers).
- Follow `.claude/specs/production-readiness-plan.md` §0.6 conventions (thin routes, `ok()/err()`, tz-aware
  datetimes, loggers with `extra={...}` and never a reserved key such as `message`, injectable clocks).
- Step 0: copy this plan to `.claude/specs/gateway-automation-plan.md` and tick tasks there as you finish them.

## Tasks (in order)

### T0 — Keep the Gateway installers out of git  [S]  (do first)
- [x] Done
- **File:** `.gitignore`. Next to the existing `IB Gateway*/` line add:
  ```
  # IB Gateway standalone installers (~300 MB each); the Docker image ships its own Gateway
  ibgateway-*.sh
  ibgateway-*.dmg
  ibgateway-*.exe
  ```
- Do not delete, move or rename the installers (they are the user's manual fallback).
- **Done when:** `git status --short` no longer lists them, and `git check-ignore -v` names the new rule for
  each of the four files. The Docker build contexts are `./backend` and `./frontend`, so images are unaffected.

### T1 — One restart-time setting, shared by Gateway and backend  [S]
- [x] Done
- **Files:** `backend/app/config.py`, `.env.example`, `.env.prod.example`, `backend/tests/unit/test_config.py`
- Add `ib_auto_restart_time: time = time(19, 0)` (ET). Env var `IB_AUTO_RESTART_TIME` accepts the Gateway's
  format `"07:00 PM"` as well as `"19:00"`, via a `field_validator(..., mode="before")` that tries
  `datetime.strptime(v, "%I:%M %p").time()` and otherwise lets pydantic parse it.
- Add `IB_AUTO_RESTART_TIME="07:00 PM"` to both example files with a one-line comment: "Daily Gateway restart
  (ET). Sunday's restart asks for the weekly IBKR Mobile approval, so pick a time you're awake. Keep it after
  16:45 ET and before 23:00 ET (IBKR's nightly reset)."
- **Tests:** `"07:00 PM"` → `time(19, 0)`; `"19:00"` → `time(19, 0)`; `"7pm"` → `ValidationError`.

### T2 — Compose: auto-login Gateway in prod and in dev  [S]
- [x] Done
- **`docker-compose.prod.yml`** (`ib-gateway` service): image `ghcr.io/gnzsnz/ib-gateway:10.50.1f`;
  `AUTO_RESTART_TIME: ${IB_AUTO_RESTART_TIME:-07:00 PM}`. In the `backend` service environment add
  `IB_AUTO_RESTART_TIME: ${IB_AUTO_RESTART_TIME:-07:00 PM}` so both read one value. Keep backend
  `depends_on: ib-gateway: service_started` (the backend must start and report "waiting for login").
- **`docker-compose.yml`** (dev): add an `ib-gateway` service under `profiles: ["ibkr"]`: same image and env
  as prod (credentials from the root `.env` via `${IBKR_USERNAME}`, `${IBKR_PASSWORD}`, `${VNC_PASSWORD}`),
  `restart: unless-stopped`, ports `"127.0.0.1:4002:4004"` and `"127.0.0.1:5900:5900"`. A backend run on the
  host keeps `IBKR_HOST=127.0.0.1`, `IBKR_PORT=4002` unchanged. The native Gateway app must be quit first
  (both want port 4002).
- **Done when:** `docker compose -f docker-compose.prod.yml --env-file <dummy env in scratchpad> config` and
  `docker compose --profile ibkr config` both validate; without `--profile ibkr` the dev stack is unchanged.

### T3 — Backend: broker connection state and a "what to do" hint  [M]
- [x] Done
- Deviation: also deleted `backend/tests/unit/brokers/test_ibkr_reconnect.py`. Commit 119b146 brought it back; it tests the reconnect loop removed in B2 and failed 13 tests on master. `test_ibkr_supervisor.py` covers the supervisor.
- **Files:** `backend/app/brokers/base.py`, new `backend/app/brokers/ibkr/hints.py`,
  `backend/app/brokers/ibkr/client.py`, `backend/app/dependencies.py` (pass the setting; fix the docstring at
  ~54-55 that says ENVIRONMENT picks the broker; it is `BROKER`).
- `base.py`: add a Pydantic model and a **non-abstract** default method (MockBroker inherits it):
  ```python
  class BrokerConnectionInfo(BaseModel):
      state: Literal["connected", "reconnecting", "down", "account_mismatch"]
      down_since: datetime | None = None
      last_connected_at: datetime | None = None
      last_error: str | None = None
      hint: str | None = None

  def connection_info(self) -> BrokerConnectionInfo:
      """Connection state for operators; brokers with richer state override this."""
      return BrokerConnectionInfo(state="connected" if self.is_connected() else "down")
  ```
- `hints.py`: one pure function, `login_hint(now_et: datetime, restart_time: time) -> str`:
  - Sunday, within 60 min after `restart_time`: "Weekly IBKR login: open IBKR Mobile and tap Approve."
  - Any other day, within 15 min after `restart_time`: "Daily Gateway restart in progress; it reconnects by
    itself within a few minutes."
  - Otherwise: "1) Check IBKR Mobile for a login request and tap Approve. 2) No request? Restart the Gateway:
    scripts/bot.sh restart-gateway. 3) Still down: open the Gateway screen over VNC (DEPLOYMENT.md)."
- `client.py`:
  - Constructor gains `clock: Callable[[], datetime] = market_calendar.now_et` and reads
    `settings.ib_auto_restart_time`. New fields: `_down_since`, `_last_connected_at`, `_last_error` (UTC
    datetimes / str), `_account_mismatch: bool`.
  - Set them where state changes: `_connect_once` success → `_last_connected_at = now`, clear `_down_since`
    and `_last_error`; first failure in `connect()` or `_supervise` → `_down_since = now` (if unset),
    `_last_error = str(exc)`; `LiveTradingGuardError` (both places) → `_account_mismatch = True`.
    Keep the monotonic `loop.time()` logic in `_supervise` for the alert threshold as is.
  - `connection_info()` override: `connected` if `self._ib.isConnected()`; `account_mismatch` if flagged;
    `down` if down for ≥ `alert_after_seconds`; else `reconnecting`. `hint = login_hint(...)` unless connected.
  - The `BROKER_DISCONNECTED` alert message becomes
    `f"IB Gateway down {minutes} min. {hint}"`, with `hint=hint` as an extra detail. SMS keeps only the first
    100 characters (`dispatcher.py:230`), so each hint must start with the action ("open IBKR Mobile and tap
    Approve" / "Check IBKR Mobile…").
- **Tests:** new `tests/unit/brokers/test_ibkr_hints.py` (Sunday 19:05 ET → weekly text; Wednesday 19:03 →
  daily text; Wednesday 11:00 → the 3-step text). In `tests/unit/brokers/test_ibkr_supervisor.py` (reuse its
  `FakeIB(fail_times=..., accounts=...)` and `_settings()`, fast backoff like the existing tests): initial connect fails → `reconnecting` with `down_since` and
  `last_error`; past the alert threshold → `down`, and the published alert text contains the hint; a later
  success → `connected` with `last_connected_at`; account `U…` in paper mode → `account_mismatch`.
  MockBroker → `connected`/`down` from the default method.

### T4 — API: expose it  [S]
- [x] Done
- Deviation: `tests/unit/api/test_layer8.py` fixture also needed `connection_info` configured (a `MagicMock(spec=BaseBroker)` otherwise returns a mock and the health endpoint 500s).
- `backend/app/api/v1/trading.py` `/trading/status`: add `"broker": engine.broker.connection_info().model_dump(mode="json")`.
  Keep `broker_connected` (the frontend chip uses it).
- `backend/app/api/v1/system.py` `/system/health`: broker component gets `info` = the same dump and
  `detail` = the hint when not connected.
- **Tests:** extend `tests/unit/api/test_trading_controls.py` and `tests/unit/api/test_system_health.py` for
  the new fields (connected and down cases).

### T5 — Make the startup "wrong account" alert actually reach the user  [S]
- [x] Done
- `backend/app/core/engine.py` `start()`: on `LiveTradingGuardError`, the alert is published before the
  notifier subscribes, so it is never emailed (Redis pub/sub keeps nothing). In that branch, also call
  `self.dispatcher.notify_system_alert(...)` directly (`backend/app/notifications/dispatcher.py:218`; match
  its signature). Wrap it so a send failure only logs.
- **Test:** unit test with a broker whose `connect()` raises `LiveTradingGuardError` and a fake dispatcher:
  the dispatcher receives `BROKER_ACCOUNT_MODE_MISMATCH` once.

### T6 — Dashboard banner driven by server state  [M]
- [x] Done
- Deviation: `SystemHealthPanel.tsx` unchanged; it already renders `detail` (truncated, full text on hover). `WatchlistPanel.tsx` also switched to `useTradingStatus` (it had a third copy of the same query).
- **Files:** `frontend/src/types/api.ts` (`BrokerConnection` type; `TradingStatus.broker`), new
  `frontend/src/hooks/useTradingStatus.ts` (the existing `useQuery` from `TradingControlsPanel.tsx`, key
  `["trading-status"]`, 15 s refetch), `frontend/src/components/controls/TradingControlsPanel.tsx` (use the
  hook), new `frontend/src/components/controls/BrokerConnectionBanner.tsx`, `frontend/src/pages/Dashboard.tsx`
  and `frontend/src/pages/SystemHealth.tsx` (render the banner at the top),
  `frontend/src/components/dashboard/SystemHealthPanel.tsx` (show the broker `detail` text under its row).
- Banner: hidden when `connected`. `reconnecting` → amber "Reconnecting to IB Gateway…". `down` → red
  "IB Gateway not connected for N min" + the hint + "Last connected: <local time>". `account_mismatch` → red
  "IB Gateway is logged into a non-paper account. Trading is blocked. Log it into the paper account."
  Reuse the panel styles already in `TradingControlsPanel.tsx`. No new packages.
- **Done when:** `npm run typecheck && npm run lint && npm run build` pass.

### T7 — One script to run the bot  [S]
- [x] Done
- New `scripts/bot.sh` (POSIX `sh`, `set -eu`, `cd "$(dirname "$0")/.."`), wrapping
  `docker compose -f docker-compose.prod.yml --env-file .env.prod`:
  `up` (`up -d --build`), `down`, `status` (`ps` + broker state read from
  `docker compose ... exec -T backend python -c "<urllib GET http://127.0.0.1:8000/api/v1/trading/status>"`),
  `logs [service]` (`logs -f --tail 200`), `restart-gateway` (`restart ib-gateway`). Unknown command → usage.
- **Done when:** `sh -n scripts/bot.sh` passes and `scripts/bot.sh` with no args prints usage.

### T8 — Docs  [S]
- [x] Done
- `DEPLOYMENT.md`: first boot = fill `.env.prod` (incl. `IB_AUTO_RESTART_TIME`) → `scripts/bot.sh up` →
  approve the push on the phone. "Weekly approval": Sunday at the restart time; missed it? The Gateway retries
  and re-sends the push; after 5 min you get an email with the steps. "Running on a Mac (MacBook)": quit the
  native Gateway app; Docker Desktop → Settings → General → "Start Docker Desktop when you sign in"; keep the
  laptop plugged in with the lid open (a closed lid sleeps a MacBook unless an external display is attached);
  System Settings → Battery → Options: prevent automatic sleeping on power adapter when the display is off;
  stay logged in (Docker Desktop is a user app). "If auto-login stops working": IBC is retired; check gnzsnz
  release notes / issue #366 before bumping the tag. Fallback in order: (1) log in by hand over VNC;
  (2) install the native Gateway for the host OS from the downloaded installer (prefer IBKR's **stable**
  channel for unattended use; the downloaded files are the **latest** channel), log in, and point the
  backend at it (`IBKR_HOST=127.0.0.1` for a backend on the host, `host.docker.internal` for one in Docker;
  `IBKR_PORT=4002`).
- `README.md` IB Gateway section: Docker path first (`docker compose --profile ibkr up -d ib-gateway` for
  development, `scripts/bot.sh up` for the full stack); the native app becomes a short fallback note that
  says the installers are gitignored and are not needed for the Docker path.
- `.claude/specs/ibkr-gateway.md`: replace the manual "Daily Startup Procedure" with the Docker flow; add the
  IBC retirement note. `.claude/specs/environment-setup.md`: fix "IBKR_HOST is always 127.0.0.1" and add
  `IB_AUTO_RESTART_TIME`.

Parallel-safe: T7 and T8 after T2; T6 after T4. Everything else in order.

Deliberately skipped: a separate Sunday reminder email (the IBKR push is the reminder, and the 5-minute
alert covers a missed one); a Gateway container healthcheck (`bot.sh status` reads the backend's view instead);
Docker secrets for the password (the gitignored env file is fine for a personal paper account); auto-detecting
"waiting for 2FA" from Gateway logs (would need the Docker socket in the backend).

## Verification

1. Backend: `cd backend && ruff check . && python3 -m pytest tests/unit -q` (new tests from T1, T3, T4, T5),
   then the integration suite with the `_test` exports from the production-readiness plan §0.2.
2. Frontend: `cd frontend && npm run typecheck && npm run lint && npm run build`.
3. Compose: both `config` commands from T2; `sh -n scripts/bot.sh`. Git: `git status --short` shows none of
   the four installer files (T0).
4. Manual, with the user's paper login and phone (the implementer cannot do this alone):
   - Quit the native Gateway app. `docker compose --profile ibkr up -d ib-gateway`, approve the push in
     IBKR Mobile, start the backend on the host (`BROKER=ibkr`, port 4002). `/api/v1/system/health` shows
     broker ok; the dashboard has no banner.
   - `docker compose stop ib-gateway`: within 15 s the banner shows "Reconnecting"; after 5 min it shows
     "not connected" with the steps, and the `BROKER_DISCONNECTED` email contains the same hint.
   - `docker compose start ib-gateway`: the banner disappears, and `BROKER_RECONNECTED` arrives.
   - First Sunday after switching: the push arrives at the configured time; approve it; Monday trading
     works without touching anything.
