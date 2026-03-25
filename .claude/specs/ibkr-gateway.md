# ibkr-gateway.md — IB Gateway Setup & Authentication

> This document covers everything needed to run IB Gateway locally for development,
> understand how authentication works, and prepare for eventual cloud deployment.
>
> Referenced by: `tasks.md § Layer 4 — Broker Abstraction`

---

## How IB Gateway Authentication Works

There are **no API keys or tokens** with the TWS API. This is different from every modern REST API you've probably used. Authentication works at the Gateway level, not the application level.

IB Gateway is a Java desktop application — a stripped-down version of the full TWS trading platform. It handles your IBKR session. Your Python code (via `ib_async`) simply connects to a TCP socket that Gateway exposes on `localhost:4001`. Gateway itself is what's authenticated with IBKR's servers.

```
You log into IB Gateway (username + password + 2FA phone tap)
        ↓
Gateway holds an authenticated IBKR session
        ↓
Your Python app connects to Gateway via TCP socket on localhost:4001
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

### Daily Startup Procedure (every dev session)

Gateway does not stay authenticated across restarts. IBKR also performs a mandatory daily system restart between **00:15–01:45 ET** which disconnects all sessions. Each time you sit down to work:

1. Launch IB Gateway
2. Enter your **paper trading** username and password
3. Approve the 2FA push notification on IBKR Mobile
4. Gateway is now running — leave it open in the background
5. Your FastAPI app can now connect via `ib_async` on `localhost:4001`

The application's reconnect logic (task 17.1) handles the case where Gateway drops and reconnects mid-session, but it cannot re-authenticate on your behalf — that always requires the 2FA tap.

---

### Verifying Gateway Is Ready

Before starting the application, confirm Gateway is listening:

```bash
# macOS / Linux
nc -zv localhost 4001

# Windows (PowerShell)
Test-NetConnection -ComputerName localhost -Port 4001
```

You should see a successful connection. If it fails, Gateway isn't running or the port is wrong.

You can also do a quick sanity check with Python once `ib_async` is installed:

```python
import asyncio
from ib_async import IB

async def check():
    ib = IB()
    await ib.connectAsync('127.0.0.1', 4001, clientId=99)
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
| API port | Gateway → Configure → API → Settings | `4001` (default) |
| Read-Only API | Gateway → Configure → API → Settings | **Unchecked** |
| Trusted IPs | Gateway → Configure → API → Settings | `127.0.0.1` (default, fine for local) |
| Socket port for TWS (if using TWS instead) | Edit → Global Configuration → API → Settings | `7497` |
| Trading mode | Set at login screen | **Paper** during development |

---

### clientId — What It Is and Why It Matters

Every connection to Gateway requires a `clientId` — an integer you assign. It identifies which client is talking to Gateway.

```python
await ib.connectAsync('127.0.0.1', 4001, clientId=1)
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
| **Gateway port** | `4001` (same) | `4001` (same) |
| **Orders** | Simulated — no real money moves | Real orders, real money |
| **Market data** | Same real-time feed | Same real-time feed |
| **Account balance** | Simulated starting balance | Real account balance |

Switch between paper and live by changing which credentials you use at the Gateway login screen. The application code is identical — only the credentials and trading mode change. This is controlled via the `IBKR_TRADING_MODE` environment variable — see `environment-setup.md § IBKR_TRADING_MODE`.

---

## The 2FA Constraint

IBKR currently requires 2FA (via IBKR Mobile) for every login. There is no way to fully bypass this for a live account. This is a deliberate security decision on IBKR's part.

**What this means in practice:**

- **Local dev**: Not a problem. You're at your computer, you tap your phone, done. Gateway stays authenticated all day.
- **Cloud deployment**: Requires a person to manually approve the 2FA prompt once per session (after the daily restart). The recommended tooling for this is IBC + `ib-gateway-docker` (see Cloud Deployment section below).

**IBC** (Interactive Brokers Controller) is a separate open-source tool that automates Gateway's login form — it fills in your username and password automatically so you don't have to type them. However, it cannot tap your phone for you. It can re-present the 2FA prompt repeatedly until you approve it.

---

## Cloud Deployment (Future Reference)

When moving to a cloud environment, the recommended approach is the community `ib-gateway-docker` image, which bundles:

- IB Gateway (the application itself)
- IBC (automated login form filling)
- Xvfb (a virtual display — required because Gateway is a GUI application)
- VNC server (optional, for remote visual access during troubleshooting)

The image is configured entirely via environment variables:

```yaml
# docker-compose excerpt (for future cloud use)
ib-gateway:
  image: ghcr.io/gnzsnz/ib-gateway:stable
  environment:
    TWS_USERID: ${IBKR_USERNAME}
    TWS_PASSWORD: ${IBKR_PASSWORD}
    TRADING_MODE: paper          # or: live
    AUTO_RESTART_TIME: "11:59 PM"
    VNC_SERVER_PASSWORD: ${VNC_PASSWORD}
  ports:
    - "127.0.0.1:4001:4001"     # API port — localhost only
    - "127.0.0.1:5900:5900"     # VNC — localhost only, tunnel if needed
```

When this container starts, IBC fills in your credentials automatically. Gateway then sends a 2FA push to your phone — you tap it once, and the session is live. IBC keeps the session alive and handles the daily restart.

For the local setup described in this document, none of this Docker complexity is needed. Keep it simple locally, introduce the container when you actually deploy.

---

## Troubleshooting

**Connection refused on port 4001**
Gateway isn't running, or hasn't finished starting up. Wait a few seconds after login and try again.

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
