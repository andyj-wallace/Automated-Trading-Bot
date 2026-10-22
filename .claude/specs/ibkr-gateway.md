# ibkr-gateway.md — IB Gateway Setup & Authentication

> This document covers everything needed to run IB Gateway locally for development,
> understand how authentication works, and prepare for eventual cloud deployment.
>
> Referenced by: `tasks.md § Layer 4 — Broker Abstraction`

---

## How IB Gateway Authentication Works

There are **no API keys or tokens** with the TWS API. This is different from every modern REST API you've probably used. Authentication works at the Gateway level, not the application level.

IB Gateway is a Java desktop application — a stripped-down version of the full TWS trading platform. It handles your IBKR session. Your Python code (via `ib_async`) simply connects to a TCP socket that Gateway exposes on `localhost:4002`. Gateway itself is what's authenticated with IBKR's servers.

```
You log into IB Gateway (username + password + 2FA phone tap)
        ↓
Gateway holds an authenticated IBKR session
        ↓
Your Python app connects to Gateway via TCP socket on localhost:4002
        ↓
ib_async sends and receives messages over that socket
```

Your application code never touches your IBKR credentials.

---

## Local Development Setup

### What You Need

- IB Gateway installed (offline/standalone version — see note below)
- An IBKR Pro account
- A paper trading account configured in Client Portal
- IBKR Mobile app installed on your phone (required for 2FA)

> ⚠️ **Offline installer required.** Always download the *offline* (standalone) installer from
> the IBKR website, not the self-updating "online" version. The self-updating version is
> incompatible with IBC (the automation tool used for cloud deployment later).
> The Gateway installer does not have a self-updating variant, so any Gateway download is fine.

---

### One-Time Configuration (already completed ✅)

These steps only need to be done once after installation:

- [x] Download and install IB Gateway (offline version)
- [x] Set up paper trading account credentials in Client Portal
  - Client Portal → head/shoulders icon → Settings → Paper Trading Account
  - Note your paper trading username (different from your live username)
  - Reset paper trading password if you haven't already — it needs its own password
- [x] Launch Gateway and log in with paper trading credentials
- [x] Disable Read-Only API mode
  - Gateway → Configure → Settings → API → Settings
  - Uncheck **"Read-Only API"** (enabled by default; blocks all order placement)

---

### Starting the Gateway (Docker, no manual login)

Run the Gateway in Docker instead of opening the app. IBC inside the image types the paper username and password from `.env` and restarts the Gateway daily at `IB_AUTO_RESTART_TIME` without asking for 2FA:

```bash
docker compose --profile ibkr up -d ib-gateway   # API on 127.0.0.1:4002, VNC on 127.0.0.1:5900
```

Quit the native app first (same login, same port 4002). Approve the IBKR Mobile push the first time and then about once a week (the first login after Sunday 01:00 ET, i.e. Sunday's restart). The backend reconnects by itself after every restart; if the Gateway stays down, the dashboard banner and the `BROKER_DISCONNECTED` alert say what to do. IBKR's nightly server reset (about 00:15–01:45 ET) also drops the connection briefly; that needs nothing from you.

Native app (fallback only): launch IB Gateway, log in with the **paper** username and password, approve the push, and leave it open. It does not restart or log back in by itself.

---

### Verifying Gateway Is Ready

Before starting the application, confirm Gateway is listening:

```bash
# macOS / Linux
nc -zv localhost 4002

# Windows (PowerShell)
Test-NetConnection -ComputerName localhost -Port 4002
```

You should see a successful connection. If it fails, Gateway isn't running or the port is wrong.

You can also do a quick sanity check with Python once `ib_async` is installed:

```python
import asyncio
from ib_async import IB

async def check():
    ib = IB()
    await ib.connectAsync('127.0.0.1', 4002, clientId=99)
    print("Connected:", ib.isConnected())
    summary = await ib.accountSummaryAsync()
    print("Account entries:", len(summary))
    ib.disconnect()

asyncio.run(check())
```

---

### Key Settings Reference

| Setting | Location | Value for Dev |
|---------|----------|---------------|
| API port | Gateway → Configure → API → Settings | `4002` (paper), `4001` (live; never used by this bot) |
| Read-Only API | Gateway → Configure → API → Settings | **Unchecked** |
| Trusted IPs | Gateway → Configure → API → Settings | `127.0.0.1` (default, fine for local) |
| Socket port for TWS (if using TWS instead) | Edit → Global Configuration → API → Settings | `7497` |
| Trading mode | Set at login screen | **Paper** during development |

---

### clientId — What It Is and Why It Matters

Every connection to Gateway requires a `clientId` — an integer you assign. It identifies which client is talking to Gateway.

```python
await ib.connectAsync('127.0.0.1', 4002, clientId=1)
```

Rules:
- Each simultaneous connection to the same Gateway instance must use a **different** `clientId`
- If two connections use the same ID, the second one will be rejected
- The ID is arbitrary — any integer works
- Use `clientId=1` for your main application, and higher numbers (e.g. `99`) for test scripts

This is configured in `.env` — see `environment-setup.md § IBKR_CLIENT_ID`.

---

### Connection Timing Gotcha

After calling `connectAsync()`, Gateway sends a `connectAck` callback immediately, but the connection is **not yet ready to use**. You must wait for the `nextValidId` callback before sending any messages — anything sent before it can be silently dropped by Gateway.

`ib_async` handles this for you automatically when you use `connectAsync()` (it awaits the ready state internally). This is only a concern if you ever drop down to lower-level Gateway interaction.

---

## Paper vs Live Mode

| | Paper | Live |
|-|-------|------|
| **Use for** | All development and testing | Production only, never during dev |
| **Credentials** | Separate paper username/password | Your main IBKR credentials |
| **Gateway port** | `4002` native / `4004` ib-gateway-docker | `4001` native / `4003` ib-gateway-docker (never used by this bot) |
| **Orders** | Simulated — no real money moves | Real orders, real money |
| **Market data** | Same real-time feed | Same real-time feed |
| **Account balance** | Simulated starting balance | Real account balance |

Switch between paper and live by changing which credentials you use at the Gateway login screen. The application code is identical — only the credentials and trading mode change. This is controlled via the `IBKR_TRADING_MODE` environment variable — see `environment-setup.md § IBKR_TRADING_MODE`.

---

## The 2FA Constraint

IBKR requires 2FA (via IBKR Mobile) for a full login. No tool can bypass it.

**What this means in practice:** with the Docker Gateway, a daily restart (`AUTO_RESTART_TIME`) logs back in without 2FA, so you approve a push about once a week: the first login after Sunday 01:00 ET. Set `IB_AUTO_RESTART_TIME` (default 7:00 PM ET) to a time you're awake on Sundays. If you miss the push, the Gateway retries and sends another (`TWOFA_TIMEOUT_ACTION: restart`).

**IBC** (Interactive Brokers Controller) is the open-source tool inside the image that fills in the login form and handles the daily restart. It cannot tap your phone for you. IBC was retired upstream on 2026-09-01; the image (pinned at `10.50.1f`) still ships its last version, IBC 3.24.2, and the image maintainer has not picked a replacement yet (gnzsnz/ib-gateway-docker#366). If auto-login ever breaks, see DEPLOYMENT.md §9.

---

## Docker Gateway (development and production)

The community `ib-gateway-docker` image (`ghcr.io/gnzsnz/ib-gateway`, amd64 and arm64) bundles:

- IB Gateway (the application itself)
- IBC (automated login form filling)
- Xvfb (a virtual display — required because Gateway is a GUI application)
- VNC server (optional, for remote visual access during troubleshooting)

The image is configured entirely via environment variables:

The production stack runs the container for you: see `docker-compose.prod.yml` and `DEPLOYMENT.md`. In that stack the backend reaches the Gateway at `ib-gateway:4004` (paper). Ports 4001/4002 listen only inside the container; the only published port is VNC on `127.0.0.1:5900`.

When this container starts, IBC fills in your credentials automatically. Gateway then sends a 2FA push to your phone — you tap it once, and the session is live. IBC keeps the session alive and handles the daily restart.

For development, `docker-compose.yml` has the same service under the `ibkr` profile, published on `127.0.0.1:4002` so a backend on the host connects exactly as it would to the native app.

---

## Troubleshooting

**Connection refused on port 4002**
Gateway isn't running, hasn't finished starting up, or is waiting for you to approve the login in IBKR Mobile. `docker compose logs ib-gateway` shows where the login is.

**"Port 4002 is already allocated" when starting the Docker Gateway**
The native Gateway app is still running. Quit it.

**Connected but immediately disconnected**
Usually a `clientId` conflict — another process is already connected with the same ID. Check for lingering Python processes and use a different `clientId`.

**Orders rejected silently**
Read-Only API is still enabled. Gateway → Configure → API → Settings → uncheck "Read-Only API".

**"No security definition has been found" error**
The ticker/contract you're requesting doesn't exist or isn't specified precisely enough. US stocks need `secType="STK"`, `exchange="SMART"`, `currency="USD"`.

**Daily disconnect at ~00:15 ET**
Expected. IBKR performs mandatory server maintenance nightly. Gateway will disconnect and reconnect automatically after the maintenance window ends (~01:45 ET). The application's reconnect logic handles re-establishing the socket connection after Gateway comes back.

**2FA prompt not appearing on phone**
Check that IBKR Mobile is installed and notifications are enabled. You can also approve it from the Client Portal website as a fallback.

---

> See `environment-setup.md` for all environment variable definitions and local `.env` setup.
> See `design.md § Security Considerations` for the live trading guard specification.
> See `tasks.md § Layer 4` for when IBKRClient is implemented.
