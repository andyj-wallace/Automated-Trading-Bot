# Roadmap

Open work only. When something is finished, delete it from this file.

The [architecture review](docs/architecture-review.md) records the November 13, 2026 review of commit `228eadd`. Finding IDs below refer to that evidence; adding a task does not mean its safeguard is implemented. Prioritize execution correctness, shared authorization and trustworthy small-account validation before adding AI or strategy families.

## Beta server: CI/CD to the home box (current focus)

Goal: merging a pull request from `master` into a protected `prod` branch builds, tests, publishes and deploys that code to the always-on home box. The box is an AMD Ryzen (x86_64), the same architecture GitHub's runners build for. Paper trading only. The "Paper soak" below runs on this box.

How it fits together:
- `master` is where work lands, through pull requests that pass CI (`.github/workflows/ci.yml`, unchanged).
- `prod` is the release branch. It changes only through a pull request from `master`, and every push to it is a release.
- New `.github/workflows/deploy.yml` runs only on pushes to `prod`. It has two jobs:
  - `publish`, on a GitHub-hosted runner, builds the backend and frontend images and smoke-tests the backend on a fresh database. It then pushes both to GitHub Container Registry (GHCR) as `sha-<first 12 characters of the commit>`.
  - `deploy`, on the box's self-hosted runner, runs `scripts/bot.sh deploy sha-…` in the box's clone. That pulls the images, restarts only `backend` and `frontend`, checks health and leaves automated entries paused.
- `scripts/bot.sh rollback`, run on the box, returns to the previous version.

Decisions already made (don't revisit):
- **`rollback` is the one deliberate use of `--force`.** It always skips the market-hours guard, because it is run by hand on the box to undo a bad release, possibly during the day. Entries stay paused (the pause lives in Redis), so skipping the guard doesn't start trading. Workflows never pass `--force`.
- **The box's runner only ever runs `deploy.yml` for pushes to `prod`.** The repo is public. Any workflow can ask for a self-hosted runner by its labels, including one added in an outsider's pull request, and branch rules don't stop that because pull-request workflows run before any merge. A pre-job hook on the box (task 6) refuses every other job. GitHub runs the hook after a job is assigned and before its first step, and a non-zero exit fails the job without running it. Runner groups, which could restrict workflows, exist only for organizations.
- **Deploys never recreate `ib-gateway`, `db` or `redis`.** Recreating the Gateway forces a new login and possibly an IBKR Mobile approval. Changes to those three services are applied by hand outside market hours (DEPLOYMENT.md section 7).
- **Deploys leave automated entries paused.** The user resumes after the DEPLOYMENT.md section 5 checks. Until AR-03 lands, startup keeps trading after a failed reconciliation.
- **The market-hours guard fails the deploy job during market hours.** Merge into `prod` after the close, or use "Re-run failed jobs" after the close. Workflows never pass `--force`.
- **Undo a release with `scripts/bot.sh rollback` on the box, not by reverting `prod`.** A revert that removes a migration can't start against the already-migrated database. The next normal release brings `prod` back in line.

Guardrails for this section:
- Don't edit the AGENTS.md do-not-touch files: `.github/workflows/ci.yml`, `.env.example`, `.env.prod.example`, existing migrations.
- Paper only. Don't change `TRADING_MODE`, `IBKR_TRADING_MODE` or any Gateway port.
- No new dependencies and no third-party GitHub actions. Use only `actions/checkout@v4` and the docker CLI on the runner.
- The only GitHub secret is the built-in `GITHUB_TOKEN`, used by `publish` alone. The `deploy` job gets `permissions: {}`. Secrets stay in `.env.prod` on the box.
- Never read `.env` or `.env.prod`. One backend replica only.

Order: tasks 1, 2 and 6 are done by the user by hand. Tasks 3, 4, 5 and 8 land on `master` through pull requests. Task 7 is the first release. The YAML, shell and hook code below are patterns that haven't been run yet. Check each piece as you build it; task 7 is the end-to-end proof.

### 1. [ ] Set up the box (user, by hand)

1. Install Ubuntu Server 24.04 LTS. In the BIOS, turn on "Restore on AC power loss". Use wired networking with a DHCP reservation in the router.
2. Install Docker Engine and the compose plugin from Docker's apt repository (not the snap). Run `sudo systemctl enable docker`.
3. Run `sudo timedatectl set-timezone America/New_York`; the cron lines in DEPLOYMENT.md are in ET. Confirm `timedatectl` shows "NTP service: active".
4. Turn on `unattended-upgrades` and allow automatic reboots only on weekends.
5. Allow SSH with keys only, and set `ufw` to block all inbound traffic except SSH. Install Tailscale.
6. Create the user that owns the clone and runs the runner, then clone:
   ```sh
   sudo adduser --disabled-password --gecos "" bot && sudo usermod -aG docker bot
   sudo git clone https://github.com/andyj-wallace/Automated-Trading-Bot.git /opt/trading-bot
   sudo chown -R bot: /opt/trading-bot
   ```
   As `bot`, in `/opt/trading-bot`, do DEPLOYMENT.md section 1 steps 1-2 (`.env.prod`, `deploy/nginx/.htpasswd`).
7. Stop the Mac's dev Gateway whenever the box's Gateway is running. Both use the same paper login, and IBKR allows one session per login.

Done when `docker run --rm hello-world` works as `bot` after a reboot, without anyone logging in.

### 2. [ ] GitHub repository settings (user, by hand)

1. Push everything pending on `master` first: once the ruleset is on, direct pushes are rejected. Then create `prod` from it: `git push origin master:prod`.
2. Settings → Rules → Rulesets → New branch ruleset, named "master and prod":
   - Enforcement: Active. Target branches: `master` and `prod`. Bypass list: empty.
   - Rules: Restrict deletions; Block force pushes; Require a pull request before merging, with 0 required approvals (GitHub doesn't let you approve your own pull request).
   - Require status checks to pass: `Backend (ruff + pytest)`, `Frontend (tsc + eslint)` and `Docker images build`.

   `ci.yml` runs its `pull_request` trigger only for `main` and `master`. A pull request into `prod` gets its three required checks from the `push` run on the same commit, which `master` already had. If `ci.yml` ever loses its `push: ["**"]` trigger, `prod` pull requests can't merge.

   From then on nothing lands on `master` or `prod` without a pull request whose CI is green. That includes the user's own commits and the agent pipeline: work on a branch and open a pull request.
3. Settings → Actions → General:
   - Fork pull request workflows from outside collaborators: "Require approval for all external contributors". This is a second layer; the hook in task 6 is the real guard.
   - Workflow permissions: "Read repository contents and packages permissions". `deploy.yml` requests `packages: write` itself.

Done when `git push origin master` of a local commit is rejected, and a pull request into `master` can't merge until CI is green.

### 3. [ ] Report the running version in `/health`

Files: `backend/app/config.py`, `backend/app/main.py`, `backend/Dockerfile`, `backend/tests/unit/api/test_system_health.py`.

1. `config.py`, in the Application block: `app_version: str = "dev"  # Set by the Docker build (APP_VERSION); not an operator setting.` Don't add it to `.env.example`: that file is do-not-touch, and operators never set this value.
2. `main.py` `health()`: return `{"status": "ok", "version": settings.app_version}`.
3. `Dockerfile`: add `ARG APP_VERSION=dev` and `ENV APP_VERSION=$APP_VERSION` after `USER appuser`. Putting them late keeps the `pip install` layer cached.
4. Test, next to `test_liveness_is_200_with_broker_disconnected`: monkeypatch `app_version` on the same `settings` object `main.py` imports, and assert that `/health` returns it as `version`.

Done when Ruff and the unit tests pass, and `docker build --build-arg APP_VERSION=sha-test -t bt ./backend` produces an image whose `/health` reports `sha-test`.

### 4. [ ] Run published images instead of building on the box

Files: `docker-compose.prod.yml`, `scripts/bot.sh`, `.gitignore`.

1. `docker-compose.prod.yml`: replace `build: ./backend` with `image: ghcr.io/andyj-wallace/trading-bot-backend:${BOT_IMAGE_TAG:-not-deployed}`, and the frontend's `build: ./frontend` with the `-frontend` equivalent. Nothing else in the file changes. `scripts/backup.sh` still runs compose without the variable; the `not-deployed` default keeps that working.
2. `.gitignore`: add `deploy/version` and `deploy/previous-version` next to `deploy/nginx/.htpasswd`.
3. `scripts/bot.sh`:
   - `compose()` passes the deployed tag: `BOT_IMAGE_TAG=$(cat deploy/version 2>/dev/null || echo not-deployed) docker compose -f docker-compose.prod.yml --env-file .env.prod "$@"`.
   - `up` refuses when `deploy/version` is missing ("Nothing deployed yet: merge into prod, or scripts/bot.sh deploy sha-<commit>") and runs `compose up -d` without `--build`.
   - New commands: `deploy sha-<12 hex> [--force]`, `rollback`, `pause [reason]`, `resume`. Add them to `usage`.
   - Move the `case` into `main()` and end the file with `main "$@"; exit $?`. Deploy checks out another commit, which replaces this file mid-run, and reading `main` in full first keeps the run consistent.

   Pattern for the new functions. Check it with `sh -n scripts/bot.sh` and on the box:

```sh
IMG=ghcr.io/andyj-wallace/trading-bot

running() { [ -n "$(compose ps -q "$1" 2>/dev/null)" ]; }

# Conservative: holidays and early closes count as open (use --force).
market_hours() {
  now=$(TZ=America/New_York date +%u%H%M)
  [ "${now%????}" -le 5 ] && [ "${now#?}" -ge 930 ] && [ "${now#?}" -lt 1600 ]
}

POST_PY='
import json, sys, urllib.request
body = json.dumps({"reason": sys.argv[2]} if len(sys.argv) > 2 else {}).encode()
req = urllib.request.Request("http://127.0.0.1:8000" + sys.argv[1], data=body, method="POST",
                             headers={"Content-Type": "application/json"})
urllib.request.urlopen(req, timeout=10).read()
'
post() { compose exec -T backend python -c "$POST_PY" "$@"; }

VERIFY_PY='
import json, sys, urllib.error, urllib.request
def get(path):
    try:
        with urllib.request.urlopen("http://127.0.0.1:8000" + path, timeout=10) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        return json.load(e)
version = get("/health").get("version")
data = get("/api/v1/system/health")["data"]
print("Version: ", version, "(expected", sys.argv[1] + ")")
for name in ("database", "redis", "engine", "broker"):
    print(f"{name:9}", data[name]["status"], data[name].get("detail") or "")
if data["broker"]["status"] != "ok":
    print("Broker not connected yet: usually the IBKR Mobile approval (DEPLOYMENT.md section 2).")
sys.exit(version != sys.argv[1] or any(data[n]["status"] != "ok" for n in ("database", "redis", "engine")))
'

wait_healthy() {
  i=0
  while [ "$i" -lt 30 ]; do
    [ "$(docker inspect -f '{{.State.Health.Status}}' "$(compose ps -q backend)" 2>/dev/null)" = healthy ] && return 0
    i=$((i + 1)); sleep 10
  done
  return 1
}

verify() {
  i=0
  while [ "$i" -lt 12 ]; do
    compose exec -T backend python -c "$VERIFY_PY" "$1" && return 0
    i=$((i + 1)); sleep 10
  done
  return 1
}

deploy() {
  version=$1 force=${2:-}
  case "$version" in
    sha-*) ref=${version#sha-} ;;
    *) echo "Version must be sha-<12 hex characters>." >&2; exit 1 ;;
  esac
  if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
    echo "Tracked files are modified. The box runs committed code only." >&2; exit 1
  fi
  if market_hours && [ "$force" != "--force" ]; then
    echo "Market hours (Mon-Fri 9:30-16:00 ET). Deploy after the close." >&2; exit 1
  fi
  docker pull -q "$IMG-backend:$version"
  docker pull -q "$IMG-frontend:$version"
  if running backend; then
    post /api/v1/trading/pause "deploy $version" || [ "$force" = "--force" ] || {
      echo "Could not pause automated entries. Fix the backend or pass --force." >&2; exit 1; }
    scripts/backup.sh
  fi
  git fetch -q origin
  git checkout -q --detach "$ref"
  expected="sha-$(git rev-parse HEAD | cut -c1-12)"
  current=$(cat deploy/version 2>/dev/null || true)
  if [ -n "$current" ] && [ "$current" != "$version" ]; then echo "$current" > deploy/previous-version; fi
  echo "$version" > deploy/version
  compose up -d --no-recreate db redis ib-gateway
  compose up -d --no-deps backend frontend
  if ! wait_healthy || ! verify "$expected"; then
    echo "Deploy of $version failed its checks. Roll back on the box: scripts/bot.sh rollback" >&2; exit 1
  fi
  post /api/v1/trading/pause "deployed $version"
  echo "Deployed $version. Automated entries are PAUSED."
  echo "Do the checks in DEPLOYMENT.md section 5, then: scripts/bot.sh resume"
}

rollback() {
  prev=$(cat deploy/previous-version 2>/dev/null) || { echo "No previous version recorded." >&2; exit 1; }
  docker pull -q "$IMG-backend:$prev"
  # The old code can't start on a database migrated past it, so downgrade with the current image first.
  old_head=$(docker run --rm -e SECRET_KEY=unused -e DATABASE_URL=postgresql+asyncpg://unused@localhost/unused \
    "$IMG-backend:$prev" alembic heads | awk 'NR == 1 { print $1 }')
  [ -n "$old_head" ] || { echo "Could not read the database revision of $prev." >&2; exit 1; }
  scripts/backup.sh
  if running backend; then post /api/v1/trading/pause "rollback to $prev" || true; fi
  compose stop backend
  compose run --rm --no-deps backend alembic downgrade "$old_head"
  deploy "$prev" --force
}
```

   In `main`, `pause` runs `post /api/v1/trading/pause "${2:-manual}"` and `resume` runs `post /api/v1/trading/resume`. Each prints one line on success.

Done when `sh -n scripts/bot.sh` passes, and `docker compose -f docker-compose.prod.yml config` (no env file; warnings are fine) shows both images with the `not-deployed` tag.

### 5. [ ] Deploy workflow

Create `.github/workflows/deploy.yml`:

```yaml
name: Deploy

on:
  push:
    branches: [prod]

permissions:
  contents: read
  packages: write

concurrency:
  group: deploy-prod
  cancel-in-progress: false

jobs:
  publish:
    name: Build, smoke-test and push images
    runs-on: ubuntu-latest
    outputs:
      tag: ${{ steps.names.outputs.tag }}
    services:
      postgres:
        image: timescale/timescaledb:2.26.1-pg15
        env:
          POSTGRES_USER: trading
          POSTGRES_PASSWORD: trading
          POSTGRES_DB: trading_bot
        ports: ["5432:5432"]
        options: >-
          --health-cmd pg_isready --health-interval 10s --health-timeout 5s --health-retries 5
      redis:
        image: redis:7-alpine
        ports: ["6379:6379"]
        options: >-
          --health-cmd "redis-cli ping" --health-interval 10s --health-timeout 5s --health-retries 5
    steps:
      - uses: actions/checkout@v4

      - name: Names
        id: names
        run: |
          TAG="sha-$(git rev-parse HEAD | cut -c1-12)"
          echo "tag=$TAG" >> "$GITHUB_OUTPUT"
          echo "TAG=$TAG" >> "$GITHUB_ENV"
          echo "IMG=ghcr.io/${GITHUB_REPOSITORY_OWNER,,}/trading-bot" >> "$GITHUB_ENV"

      - name: Build
        run: |
          SRC="org.opencontainers.image.source=https://github.com/${GITHUB_REPOSITORY}"
          docker build --build-arg APP_VERSION="$TAG" --label "$SRC" -t "$IMG-backend:$TAG" ./backend
          docker build --label "$SRC" -t "$IMG-frontend:$TAG" ./frontend

      - name: Smoke-test the backend (migrations on a fresh database, then /health)
        run: |
          docker run -d --name smoke --network host \
            -e ENVIRONMENT=development -e SECRET_KEY=smoke-test-not-secret \
            -e DATABASE_URL=postgresql+asyncpg://trading:trading@localhost:5432/trading_bot \
            -e REDIS_URL=redis://localhost:6379/0 -e BROKER=mock -e IBKR_TRADING_MODE=paper \
            "$IMG-backend:$TAG"
          for i in $(seq 1 60); do
            if curl -fsS http://localhost:8000/health | grep -q "\"$TAG\""; then exit 0; fi
            sleep 2
          done
          docker logs smoke
          exit 1

      - name: Push
        run: |
          echo "${{ secrets.GITHUB_TOKEN }}" | docker login ghcr.io -u "${{ github.actor }}" --password-stdin
          docker push "$IMG-backend:$TAG"
          docker push "$IMG-frontend:$TAG"

  deploy:
    name: Deploy on the box
    needs: publish
    runs-on: [self-hosted, trading-box]
    permissions: {}
    steps:
      - name: scripts/bot.sh deploy
        run: cd /opt/trading-bot && scripts/bot.sh deploy "${{ needs.publish.outputs.tag }}"
```

The smoke test uses `BROKER=mock` and `ENVIRONMENT=development`, so it proves migrations and startup, not that the production configuration boots (the production-only startup checks in `main.py` aren't exercised).

If the backend needs another variable to start in the smoke test, add it with a dummy value, never a real secret.

Done when the file passes review in its pull request into `master`. The first real run is task 7.

### 6. [ ] Self-hosted runner on the box, locked to `prod` deploys (user, by hand)

1. Settings → Actions → Runners → New self-hosted runner → Linux, x64. Run the commands it shows as `bot` in `/home/bot/actions-runner`. At the labels prompt, add `trading-box`. Install it as a service from that folder: `sudo ./svc.sh install bot && sudo ./svc.sh start`.
2. Install the guard hook. It's owned by root, so the runner's user can't change it:
   ```sh
   sudo mkdir -p /opt/runner-hooks
   sudo tee /opt/runner-hooks/only-prod-deploy.sh > /dev/null <<'EOF'
   #!/bin/sh
   # Refuse every job except deploy.yml running for a push to prod.
   if [ "$GITHUB_EVENT_NAME" = "push" ] && [ "$GITHUB_REF" = "refs/heads/prod" ] &&
      [ "$GITHUB_WORKFLOW_REF" = "andyj-wallace/Automated-Trading-Bot/.github/workflows/deploy.yml@refs/heads/prod" ]; then
     exit 0
   fi
   echo "Refused: this runner only runs deploy.yml for pushes to prod." >&2
   exit 1
   EOF
   sudo chmod 755 /opt/runner-hooks/only-prod-deploy.sh
   echo "ACTIONS_RUNNER_HOOK_JOB_STARTED=/opt/runner-hooks/only-prod-deploy.sh" | sudo -u bot tee -a /home/bot/actions-runner/.env
   sudo chown root: /home/bot/actions-runner/.env && sudo chmod 644 /home/bot/actions-runner/.env
   cd /home/bot/actions-runner && sudo ./svc.sh stop && sudo ./svc.sh start
   ```
3. Check the hook accepts the real case. Run it by hand with the variables a `prod` deploy would have; it must exit 0:
   ```sh
   GITHUB_EVENT_NAME=push GITHUB_REF=refs/heads/prod \
     GITHUB_WORKFLOW_REF=andyj-wallace/Automated-Trading-Bot/.github/workflows/deploy.yml@refs/heads/prod \
     /opt/runner-hooks/only-prod-deploy.sh; echo $?
   ```
   Then run it with `GITHUB_EVENT_NAME=pull_request`; it must exit 1. This doesn't prove GitHub passes those variables to the hook; task 7 does.
4. Drill: push a throwaway branch (rulesets cover only `master` and `prod`) with a workflow whose job has `runs-on: [self-hosted, trading-box]` and runs `touch /tmp/runner-guard-breached`. Expected: the job fails during setup with the "Refused" message, and `/tmp/runner-guard-breached` doesn't exist on the box. Delete the branch afterwards.

Done when the manual hook check and the drill pass and the runner shows "Idle" under Settings → Actions → Runners. The hook only fails closed: if a variable it checks were missing, every job would be refused, and task 7 would show it.

### 7. [ ] First release and drills (user and implementer)

1. Outside market hours, open a pull request from `master` into `prod` and merge it. Deploy runs `publish`, then `deploy`.
   - If `deploy` can't pull the images, that's because new GHCR packages may start private. Go to GitHub → profile → Packages → `trading-bot-backend` and `trading-bot-frontend` → Package settings → Change visibility → Public, then use "Re-run failed jobs". The images contain only the public source: CI builds them from a clean checkout, and `.dockerignore` excludes `.env` files.
   - Approve the Gateway login in IBKR Mobile. The job log shows the version line, `database`, `redis` and `engine` ok, and "PAUSED".
2. On the box, run `scripts/backup.sh` and confirm it writes a dump and an audit-log copy. If the copy fails because `audit.log` doesn't exist yet, make that step skip a missing file with a message.
3. During market hours, merge a small change into `prod`. The `deploy` job fails with the market-hours message. After the close, "Re-run failed jobs" deploys it.
4. On the box as `bot`, run `scripts/bot.sh rollback`. `/health` shows the previous version and the checks pass. Re-run the latest Deploy job to return to the current version.
5. Reboot the box. All five containers and the runner come back by themselves, and `scripts/bot.sh status` shows the broker connected (approve in IBKR Mobile if asked).
6. Add the backup cron line from DEPLOYMENT.md section 6 to `bot`'s crontab, with the path `/opt/trading-bot` (the box runs on ET).
7. Run `scripts/bot.sh resume` once satisfied.

Done when all seven pass. GitHub emails the user about failed workflow runs, so a failed deploy is noticed without extra alerting.

### 8. [ ] Docs

- `DEPLOYMENT.md`:
  - Opening: the backend and frontend images come from GHCR, built by `deploy.yml`, and merging into `prod` deploys. Add `deploy`, `rollback`, `pause` and `resume` to the `scripts/bot.sh` command list.
  - Section 1: box setup (task 1), the runner (task 6), and the first release through `prod`.
  - Section 3: retitle it "The host". The Linux box comes first. Keep the Mac bullets as a fallback, and delete the "Moving to an always-on Linux server later" bullet.
  - Section 7: release flow. A pull request into `master` passes CI. After the close, a pull request from `master` into `prod` deploys. Read the Deploy job log, do the section 5 checks, then run `scripts/bot.sh resume`. Changes to `ib-gateway`, `db` or `redis` need `docker compose -f docker-compose.prod.yml --env-file .env.prod up -d <service>` by hand, outside market hours.
  - Section 8: `scripts/bot.sh rollback`, what it does, and why reverting `prod` isn't the way back. Keep the manual restore steps for when it fails. Database changes must keep working with the previous release's code (add columns; don't rename or drop them in the same release).
- `README.md`: one line on the release flow, wherever CI is described.
- `AGENTS.md`: in Architecture notes, one sentence on `deploy.yml` and the `prod` branch. Also state that `master` and `prod` accept changes only through pull requests with green CI.

### 9. Optional hardening (any order)

- [ ] **Heartbeat** (alerts you when the whole box goes silent). Create a free healthchecks.io check, then add a cron line for `bot` every 5 minutes: `docker inspect -f '{{.State.Health.Status}}' trading-bot-backend-1 | grep -q healthy && curl -fsS -m 10 --retry 3 https://hc-ping.com/<uuid> > /dev/null`.
- [ ] **Off-host backup copy.** A nightly `rsync -a backups/ <destination>/`. Never use `--delete`: a wiped source would wipe the copy too.
- [ ] **Pin CI's TimescaleDB** to `2.26.1-pg15`. `ci.yml` is do-not-touch, so this needs the user's OK. The deploy smoke test already uses the pinned version.
- [ ] **Make the repo private.** This is the strongest protection for the runner. If you do it, keep the packages private and run `docker login ghcr.io` as `bot` with a classic token that has only `read:packages`.

Not bugs:
- Images are tagged by commit (`sha-…`). `prod`'s history is the release history; there are no version tags.
- A `deploy` job that fails during market hours is the guard working.
- The runner refusing jobs from any other workflow, event or branch is the hook working.
- `rollback` goes back one version only. It leaves `prod` at the bad commit until the next release.
- Commits from before tasks 3-5 have no images.

## Before live money (in order)

Everything here runs on the paper account. Settings are in `.env.example`; terms are explained in the README's Growth mode section.

### 0. Repair execution and deterministic authorization

The November 13 review passed 823 backend unit tests, backend lint, frontend lint and the frontend build/type check. Focused mock probes still reproduced the failures below. Add regression coverage for the invariants as each is fixed; passing existing tests is insufficient for live approval.

- [ ] **P1 / AR-03 — Fail closed on recovery and protection faults.** Add shared entry-readiness state in `engine.py` and the execution path. Failed circuit-breaker recovery, missing/stale reconciliation, unresolved mismatches and failed required protective stops must withdraw entry readiness. Done when failure-injection tests block new risk until recovery is confirmed while risk-reducing actions remain available.
- [ ] **P1 / AR-04 — Enforce execution leadership and account reservations.** Cover manual, scheduled and recovery mutations in `dependencies.py`, `order_manager.py`, `engine_lock.py` and repository operations. Reject follower mutations and fence expired leaders immediately before dispatch. Make symbol, cash and risk reservations transactional across processes; continue deploying one worker/replica. Keep automation pause distinct from an account-wide emergency halt. Done when leadership loss, a second process and concurrent proposals cannot create duplicate or over-budget exposure.
- [ ] **P1 / AR-05 — Centralize required data and account checks.** Move mandatory spread/liquidity/freshness checks into shared entry authorization. Preserve source/receipt timestamps and data mode; reject missing required quotes and account facts rather than assuming safe values. Verify settled funds and the broker/account's applicable restrictions. Done when manual and automated entries receive the same deterministic decisions for missing, stale, delayed or invalid required inputs.
- [ ] **P1 / AR-06 — Persist authorization and dispatch durably.** Replace logging-only audit success with checked durable proposal, authorization and order-intent records plus a transactional outbox. Record fills and reconciliation outcomes incrementally, with versioned input references. Preserve existing immutable audit columns and append subsequent facts. Done when failed mandatory persistence prevents dispatch and restart/retry recovers intent without duplicate execution; exercise actual handler I/O failure, not only a mocked logger method.
- [ ] **P1 / AR-07 — Budget all-in loss and actual exposure.** Include estimated entry/exit friction and execution headroom when sizing within the existing fixed 1% ceiling. Maintain current exposure separately from immutable approved risk, including adverse fills and late executions. Done when the $1,000/$10 planned-risk/$1.34 friction case is resized or rejected to meet the all-in modeled budget; report gap risk separately. Entry restrictions must never block an exit because of fees, settlement or day-trade rules.
- [ ] **P2 / AR-10 (broker outcomes) — Report broker outcomes accurately.** `scheduler_submit.py` records SUBMITTED whenever submission returns without raising, including REJECTED/ERROR results; the manual route can return HTTP 201 for the cancelled trade. Inspect the broker outcome and expose accepted, working, partial, rejected, unknown and filled states to the signal log and API/UI. Done when clean broker rejection and uncertain submission produce truthful outcomes without weakening successful-order behavior.

### 1. Make historical and paper validation representative

- [ ] **P1 / AR-08 — Share simulation and execution policy.** In `backtesting/engine.py`, `portfolio_engine.py` and `backtesting_helpers.py`, apply aggregate capital/cash reservations, growth-universe and position constraints, settlement and relevant exit rules, including signal exits. Advance portfolio slots on a shared calendar. Done when two simultaneous 40-share/$25 positions cannot consume about $2,001 on $1,000, and matching simulated/execution proposals produce explainable equivalent authorization decisions.
- [ ] **P1 / AR-09 — Add an isolated virtual paper ledger.** Keep `TRADING_CAPITAL_LIMIT`'s existing sizing-cap meaning; add explicit experimental cash/equity accounting for small-account certification. Include realized/unrealized P&L, fees, reservations and settlement. Done when a $100 experiment losing $10 has $90 of experimental equity even if broker paper equity remains near $1 million, and subsequent sizing/cash checks use that ledger.
- [ ] **P2 / AR-10 (metrics) — Correct metrics and chronological validation.** Build marked-to-market equity returns for drawdown and Sharpe in `backtesting/metrics.py`. Extend `walk_forward.py` with rolling evaluations, prior-history warm-up, extra-symbol history and consistent cost settings. Separate training/tuning from untouched evaluation data; use purging/embargo where overlapping labels require it. Done when sparse samples are inconclusive, regime-filter inputs are present, and statistics use their documented sampling period.

### 2. Validate growth mode on paper

1. [ ] Check your IBKR pricing plan (Client Portal: Settings > Account Settings > Pricing) and set `COMMISSION_PLAN`. Validate eligibility using the configured cost model, current prices and actual stop distances; the example equity thresholds in the README are not guarantees of entry.
2. [ ] Set `TRADING_CAPITAL_LIMIT=100` and `ACCOUNT_TYPE=cash` for initial rejection/operational checks. Enable `trend_pullback` with currently affordable ETFs from the configured `GROWTH_UNIVERSE`; check prices and sufficient history for each symbol and its market filter. Use the isolated virtual ledger from section 1 for small-account certification; the sizing cap alone does not reproduce account losses or settlement.
3. [ ] Run one week at 100. Confirm zero-size and fee-ineligible signals are blocked with `ZERO_QUANTITY` or `RISK_REJECTED` / `COSTS_TOO_HIGH`, respectively. Check `growth_mode` and the daily summary against policy. Record no-trade periods as valid outcomes; this interval tests behavior, not profitability.
4. [ ] Run two or more weeks at experimental capital that passes configured fee and whole-share checks. Verify ledger equity/cash, recorded commissions, net P&L, broker-confirmed protective stops and position limits. Reconcile fee estimates with execution reports and the actual pricing plan.
5. [ ] After simulation parity and metric repairs, backtest `trend_pullback` on 5 ETFs over 5 years, starting at $100, $300 and $1,000 on both fee plans. Record dataset versions, symbols, configurations, dates, trade count, turnover, time invested, net expectancy, marked-to-market drawdown, return and fees. Compare with cash and a passive benchmark, and include allocated hosting/data/AI costs. Require informative untouched out-of-sample results at the intended capital; positive in-sample return or a 45% cost-filter pass does not establish an edge.

### 3. Paper soak: 5 trading days in a row

Run the production stack (`scripts/bot.sh up`) on paper with the settings chosen above. This is operational acceptance after the repairs, not proof of profitability. Keep a dated evidence log outside the append-only broker audit log.

Check each day:

- [ ] `/api/v1/system/health` is ok during market hours, and the summary email arrived.
- [ ] No unexplained entries in `error.log`.
- [ ] Every symbol with an open trade had prices all day (no `PRICE_FEED_STALE`).
- [ ] Reconciliation completed recently and shows zero unresolved mismatches, including excess holdings, missing shares, direction, open orders and protection. A healthy component response alone does not establish trading readiness.
- [ ] Every signal in `strategy_signals` produced at most one trade.
- [ ] Every open trade has a resting GTC stop in the Gateway/TWS order list.
- [ ] Every closed trade has exit fills and P&L matching IBKR's execution report.
- [ ] The log shows a clean reconnect after IBKR's nightly restart.

Run once during the soak:

- [ ] Restart the backend during market hours with a position open. The monitor and reconciler resume, and the position stays protected.
- [ ] Stop the Gateway for 10 minutes. `BROKER_DISCONNECTED` then `BROKER_RECONNECTED` arrive, and prices resume.
- [ ] Pause: no automated entries; exits continue under their documented rules. Test the separate account-wide entry halt after implementation, including manual requests. Resume only after checking account consistency.
- [ ] Manually close one trade, and flatten everything on the last day. Verify terminal entry/exit orders, final broker quantities and commissions; a successful HTTP response alone does not prove flatness.
- [ ] Run `scripts/backup.sh` and restore the dump into a scratch database (DEPLOYMENT.md section 6).
- [ ] In an isolated test setup, inject partial fills, cancel races, duplicate delivery, mandatory audit-write failure, unavailable account data, leadership loss and failed startup recovery. Preserve evidence that no forbidden new risk was submitted and every confirmed fill remained accounted for.

### 4. Go live (separate change)

- [ ] Complete the execution, authorization, simulation and paper gates above, plus the account-risk requirements below. Prepare a separate live-deployment review covering account-mode guards, credentials, Gateway configuration, broker/account restrictions, capital limits, operating cost, monitoring and rollback. The current runbook is paper-only; passing validation does not authorize a switch. Any live deployment requires your explicit go-ahead and an approved runbook.

## Leadership architecture delivery

Proposed work toward the supplied hybrid architecture. The [review](docs/architecture-review.md#proposed-architecture) defines boundaries and evidence; these capabilities are not current runtime guarantees.

1. [ ] **Account-risk requirements before live review.** Add deterministic per-position, sector and correlated-exposure limits, marked-to-market daily-loss controls and high-water-mark drawdown controls. Define policy versions and account-level halt/reset semantics. Verify the applicable broker margin regime and missing-value behavior; do not infer account eligibility from a generic day-trade count alone.
2. [ ] **Versioned data and durable events.** Reuse PostgreSQL for decision records and an outbox, keeping Redis for cache/transient UI events. Preserve source/availability/receipt times, data mode, corporate-action adjustment policy and immutable input references. Add fundamentals or news only when the research hypothesis needs them. Done when replay reconstructs the inputs and policy behind a decision without depending on overwritten bars or missed Pub/Sub messages.
3. [ ] **Deterministic ranking and allocation.** Collect proposals before allocation, rank opportunities under an explicit policy and reserve shared capital once. Give strategies read-only data/account interfaces. Done when proposal order cannot silently over-allocate the account and every executable intent references a valid, unconsumed authorization.
4. [ ] **One ML model in shadow evaluation.** Implement features, offline training, model registry, calibration and drift/rollback diagnostics for one model before adding an ensemble. Persist model/feature/data versions and compare with the repaired rule-based baseline. Done when informative out-of-sample results demonstrate incremental benefit after trading and operating costs before influencing allocation.
5. [ ] **Bounded LLM research and explanations.** Begin with cited research/event interpretation and structured outputs, with cached results, timeouts, schema validation, versioned prompts/models and a spending ceiling. Keep broker mutation credentials/capabilities in execution. Done when invalid or unavailable required research blocks the dependent proposal under deterministic policy, and no agent can bypass authorization or reset safeguards.
6. [ ] **Full decision replay and promotion evidence.** Link input snapshots, model/research outputs, proposals, risk evaluations, authorizations, fills, fees and reconciliation. Done when a recorded decision can be reconstructed, duplicate delivery is idempotent and a promotion/rollback decision has reproducible supporting evidence.

Fill accounting and reconciliation are done; the order-state and fill-journal contract lives in `brokers/base.py` and `trade_fills`. For Claude's handoff, another developer can own readiness and authorization; agree on that contract before editing shared execution modules. These are proposed assignments, not evidence of a delivered handoff.

## Strategy families (after the validation gates)

Implements the strategy ideas in the Backlog below. Start only after "Before live money" sections 0 and 1 are done: a new strategy is worthless if backtests can't be trusted. Everything ships **disabled** (seeding already creates a disabled row per registered type) and is enabled on paper only after its backtest passes.

Order is by what each family needs. Phases 1 and 2 fit the engine as it is. Phases 3 and 4 need things that are forbidden today, so each starts with a decision from the user and stops there until it is made.

Rules for all phases:
- A strategy is one file in `backend/app/core/strategy_engine/` that subclasses `BaseStrategy`, calls `registry.register("<name>", Cls)` at the bottom, and is imported in `builtin.py`. Copy the shape of `trend_pullback.py`: config from `config.get(...)`, `ValueError` for bad config in `__init__`, `get_config_schema()` with a default for every key, `min_history_bars()`, `required_extra_symbols()`, `_hold()`, and a docstring listing the rules.
- Decide from completed daily bars (`bars[-1]` is the last completed session) and enter the next day, like Trend Pullback, so live and backtest match.
- Long only. `submit_stop_to_broker=True`. `take_profit_price` is only a strategy suggestion (the monitor handles it; never sent to the broker). Exits that are not stops use `action="SELL"` while `open_position` is true; the scheduler turns that into a `SIGNAL_EXIT`.
- Reuse `indicators.py` (`sma`, `atr`, `avg_dollar_volume`) and `filters.py` (`regime_ok`, `volatility_ok`). New shared helpers go there, not in the strategy file.
- Prices are `Decimal` built from `str`. Quantity stays `int` and comes from `RiskCalculator`.
- Tests in `backend/tests/unit/strategy/test_<name>.py`, using the helpers in that folder's `conftest.py`. Cover: each entry rule failing on its own gives HOLD; the all-pass case gives BUY with the documented stop and target; insufficient history gives HOLD; bad config raises `ValueError`; no look-ahead (appending a future bar doesn't change the signal for earlier bars).
- Add the strategy to `test_min_history_bars.py`, and update README's strategy list in the same change.
- Run `graphify update .` after code changes.

### Known limit to fix first

`scheduler_data.py` loads only `_HISTORY_LOOKBACK_DAYS = 365` calendar days of daily bars, about 250 sessions. That covers a 200-day average and a 52-week high, but not an **all-time high**. Phase 1 handles this by approximating "all-time high" with a longer window, set per strategy. Task 1.1 makes the lookback a strategy property.

### Phase 1. Rocket breakout and long momentum (no new infrastructure)

#### 1.1 [ ] Let a strategy ask for more history

Files: `strategy_engine/base.py`, `scheduler_data.py`, `core/jobs/` (bar refresh), tests under `tests/unit/strategy_engine/`.

1. `BaseStrategy.history_days(self) -> int` defaults to `0` (meaning "use the scheduler default of 365").
2. The scheduler takes the largest `history_days()` among enabled strategies for a symbol and passes it as the lookback in `_fetch_db_bars`. The bar cache key includes the lookback.
3. Check whether the bar refresh job stores that much history for each symbol. If not, extend its backfill by the same amount, only for symbols an enabled strategy needs it for.
4. Tests: default unchanged at 365; a strategy asking for 1500 gets 1500; a symbol with less history than asked still returns what exists.

Done when existing strategies see the same bars as before and a test shows a longer window arriving.

#### 1.2 [ ] `rocket_breakout`

Source: the Backlog "Rocket stocks" section. Config (all with defaults, all `Decimal` from `str`):

| Key | Default | Meaning |
|---|---|---|
| `high_lookback` | 252 | Sessions for the "52-week high" |
| `ath_lookback` | 1250 | Sessions for the "all-time high" approximation (about 5 years); `0` turns this rule off |
| `fast_period` / `slow_period` | 50 / 200 | Moving averages |
| `volume_period` | 50 | Prior average volume window |
| `volume_surge` | 1.5 | Breakout bar volume must be at least this multiple |
| `max_gap_pct` | 0.05 | HOLD if the open gapped above the prior close by more than this |
| `max_extension_pct` | 0.03 | HOLD if current price is above the breakout level by more than this |
| `atr_period` / `stop_atr_mult` / `max_stop_atr` | 14 / 2.0 / 3.5 | Stop = entry - mult * ATR; HOLD if wider than max |
| `trail_atr_mult` | 3.0 | Exit when close falls below highest close since entry minus this * ATR |
| `failed_breakout_days` / `failed_breakout_buffer_pct` | 5 / 0.0 | Exit if within N sessions of entry the close is back below the breakout level |
| `max_holding_days` | 60 | Time exit |
| `regime_symbol` | SPY | Same regime filter as Trend Pullback; `""` turns it off |

Entry (all must hold, else HOLD):
1. Breakout: `close[-1]` is above the highest high of the `high_lookback` sessions **before** `bars[-1]` (exclude the bar itself, so no look-ahead), and above the same for `ath_lookback`.
2. Trend: `close[-1] > SMA(fast) > SMA(slow)`.
3. Volume: `volume[-1] >= volume_surge * mean(volume of the volume_period sessions before bars[-1])`.
4. No gap or chase: both limits above.
5. Regime and `avg_dollar_volume` checks as in Trend Pullback and the scheduler's existing liquidity gate.

Stop and sizing as in the table. No take profit: winners are cut by the trailing exit. Exits (SELL while `open_position`): trailing exit, failed breakout, `max_holding_days`. The trailing and failed-breakout exits need the entry date and breakout level, which a stateless strategy doesn't have. Recompute them from bars: the breakout level is the highest high of the `high_lookback` sessions before the first session in the last `failed_breakout_days` where the entry rules held. If that is not reproducible from bars alone, drop the failed-breakout exit and say so in the docstring; don't add strategy state.

Tests: add the standard set plus: a gap above `max_gap_pct` gives HOLD; volume at 1.49x gives HOLD and 1.5x gives BUY; a high set inside the lookback but on `bars[-1]` itself doesn't count as "prior"; `ath_lookback=0` skips that rule; stock splits not adjusted in the bars produce a HOLD or are documented as out of scope (check what `historical.py` stores first).

Done when the tests pass and `backtest_run` on the Growth universe ETFs and five large caps runs without error. Report trade count; a handful of trades is "inconclusive", not a pass.

#### 1.3 [ ] `momentum_long`

Before writing it, read `stock_trend.py` and `trend_pullback.py` and write down in the docstring what this does that they don't. If the only difference is the ranking, build the ranking and not another entry rule. Proposed difference: **relative strength**.

- Score = total return over `lookback_days` (default 126) skipping the most recent `skip_days` (default 5), compared with the same score on `regime_symbol`'s bars (`extra_bars`). Enter only when the symbol's score beats the benchmark's by `min_rs_margin` (default 0.05) and the score is positive.
- Plus: close above `SMA(slow)`, `avg_dollar_volume` above the existing floor, volatility in range (`volatility_ok`).
- Stop: `stop_atr_mult` (2.5) x ATR below entry. Trailing exit and `max_holding_days` (90) as in rocket. Failed-trend exit: close below `SMA(slow)`.
- Cross-sectional ranking ("top 10% of the universe") is out of scope: it needs all symbols at once, which is the roadmap's "Deterministic ranking and allocation" item. Say so in the docstring.

Tests: the standard set plus relative strength below the margin gives HOLD, and the skip window excludes recent bars.

Done when tests pass and the docstring states the difference from `trend_pullback` in two lines.

#### 1.4 [ ] Compare on paper-grade evidence

No new code if the backtest engine already reports it. Run `rocket_breakout`, `momentum_long`, `trend_pullback` and buy-and-hold on the same symbols and dates, fees on, 3 starting balances ($100, $300, $1,000), with the walk-forward split from section 1 of "Before live money". Record the numbers in the pull request, not in a doc. Done when each strategy has an out-of-sample result at the capital it would run at, or a recorded reason it can't (too few trades).

### Phase 2. Dividend investing (needs data, and one rule bent)

This family holds for months and buys in pieces, which breaks two engine rules: one active trade per symbol, and a stop on every trade. **Decision for the user before any code:** keep dividend holdings outside the trade/stop machinery as a separate "holdings" ledger (recommended: simplest, can't weaken the risk rules, but needs its own tables and reconciliation), or extend trades to allow several lots per symbol. Don't start until this is answered.

Tasks, in order. Each is a separate pull request.

1. [ ] **Tax research.** Done as the Backlog describes: a short written answer with official sources and the date, pasted into the pull request. No code.
2. [ ] **Dividend data.** Pick the source first (ask the user; an IBKR dividend feed, or a free API, which is a new dependency or key). Store declared amount, ex-date, record date, pay date and frequency in a new table (migration `0013`, hand-written, working `downgrade()`). A repository in `db/repositories/`, a daily refresh in `core/jobs/`, and helpers `trailing_yield`, `forward_yield` (excludes special dividends) in a new `core/dividends.py`. Tests use fake rows; no network in tests.
3. [ ] **Detect cuts.** A pure function over the dividend history: confirmed cut = the regular amount per period falls after adjusting for frequency changes and excluding special dividends and splits; omission = a missed expected payment past a grace period. Tests for each false positive named in the Backlog (frequency change, special, split, ETF variability).
4. [ ] **Recurring buys.** A monthly job that buys whole shares with available cash only: `NOBL` one share, or one-twelfth of a stock allocation per month. Unused cash carries forward. Ladder mode has explicit price levels and a total cap; a falling price never buys past the cap. All orders go through the existing entry authorization and the 10% portfolio cap.
5. [ ] **Exit rules.** Exit an individual company the next session after a confirmed cut or omission; never exit on price or technicals. ETFs are never exited by the cut rule. Exits use the existing close path, so they are never blocked by growth mode or fees.
6. [ ] **Validation.** Backtests need historical Dividend Aristocrats membership and dividend events; if that data isn't available, say so and stop at paper. Report income, yield on cost, total return and drawdown separately.

### Phase 3. Short momentum (blocked on the user)

`.claude/CLAUDE.md` says "Never implement short selling". Lifting it is the user's call and changes risk sizing, P&L, reconciliation, the circuit breaker and the exit rules. Ask. If the answer is yes, write a separate plan first covering `TradeDirection`, `RiskCalculator` for shorts (stop above entry), borrow availability and fees, buy-to-cover exits that are never blocked, gap-through-stop handling and squeeze limits. Only then build `momentum_short` from the Backlog rules. Until then, this phase is "do nothing".

### Phase 4. Options: iron condor, covered calls, cash-secured and covered puts (blocked on the user)

None of this exists: no options data feed, contracts, multi-leg orders or margin model, and `BaseBroker` has no options methods. Covered puts also need Phase 3. **Decisions for the user:** whether to do options at all on a small account (contract multiples and fees make most of these ineligible below a few thousand dollars; check with the cost model first), and where options data comes from (IBKR's feed, which may need a market-data subscription, or other).

If yes, write a separate plan. Suggested order: (1) data feed and contract model, no trading; (2) cash-secured puts, the simplest because the collateral is just reserved cash; (3) covered calls, adding share-coverage reservation and assignment handling; (4) iron condor, adding multi-leg orders and defined-risk sizing; (5) covered puts last. Each stays paper-only until reconciled against broker assignments and expirations.

### Not bugs
- Strategies that return HOLD for weeks on a small account are the filters working.
- `rocket_breakout` skipping a gap-up is intended, not a missed trade.
- "All-time high" is approximated by `ath_lookback` until the history store goes deeper.

## Optional

Schedule hosting expansion and additional strategy families after the deterministic baseline and validation gates. Reuse infrastructure and set operating-cost limits appropriate to experimental capital.

- [ ] **Show growth mode on the Home page.** `/trading/status` returns `growth_mode` and `frontend/src/types/api.ts` has its type, but nothing displays it. Show a chip with equity and the returned `exit_equity` while `growth_mode.active` is true.
- [ ] **AWS host.** Only if you choose AWS over your Mac or another always-on machine. CDK in Python: default VPC; one t3.small EC2 instance (Amazon Linux 2023, Docker) with no inbound security-group rules; SSM Session Manager for shell and port forwarding (8080, 5900); encrypted gp3 volume with daily snapshots; an IAM role with `AmazonSSMManagedInstanceCore` and write access to one S3 backup bucket; secrets in Secrets Manager, rendered into `.env.prod` at boot.
- [ ] **Strategy cleanup.** Centralize `min_separation_pct` parsing in `filters.py`; make `min_separation_ok` return `False` when the slower value is ≤ 0; make `CompositeStrategy` return HOLD when the combined stop would be `None`; fetch extra-symbol bars with `asyncio.gather`.
- [ ] **Repository tidy-up.** Move `test_ibkr_connection.py` to `backend/scripts/check_gateway.py`. Split `requirements-dev.txt` (pytest, pytest-asyncio, pytest-cov) from the runtime requirements; the Dockerfile installs runtime only, CI installs both.

## Backlog

The strategy families below (dividend, momentum, rocket, covered calls, puts, iron condor) are planned in "Strategy families" above; this list holds the research rules that plan refers to.

- **Options (iron condor):** options data feed, multi-leg orders and database models, spread-based risk calculation.
- **Mobile PWA:** manifest and service worker, responsive layout, web push (needs HTTPS).
- **Second broker (Alpaca, Tradier):** new `BaseBroker` implementation and mapper, per-broker paper-mode guard.

### Dividend investing strategy

Build a long-term dividend-income strategy with gradual purchases and optional dividend reinvestment for compounding. Hold through price declines rather than selling on ordinary trading signals; exit individual companies when a dividend cut or omission is confirmed. These are proposed strategy rules to research and validate, not assumptions of guaranteed returns.

- [ ] **Tax research.** Research current dividend taxation for the investor's jurisdiction and account type, including qualified versus ordinary dividends, holding-period requirements, withholding and the treatment of reinvested dividends. Record official sources and the date checked.
- [ ] **Universe and ETF alternative.** Source current S&P 500 Dividend Aristocrats membership and verify the requirement for at least 25 consecutive years of dividend increases. Treat 3M, A. O. Smith (`AOS`), Aflac and AT&T as research examples, not a current approved list. Evaluate ProShares S&P 500 Dividend Aristocrats ETF (`NOBL`) as an alternative to individual stocks; verify its current holdings and expense ratio rather than hard-coding the quoted 0.35%.
- [ ] **Dividend data and eligibility.** Add dividend declarations, amounts, frequency, ex-dividend dates, record dates and payment dates. Calculate dividend yield as annual dividend per share / current share price; distinguish trailing payments from forward annualized dividends and exclude misleading special-dividend annualization. Validate purchase eligibility against the ex-dividend date, not the payment date.
- [ ] **Recurring purchases.** Support a monthly schedule funded by available cash: one share of NOBL per month, or one-twelfth of a planned stock allocation each month for twelve months. Respect whole-share execution and carry unused cash forward. Define an alternative ten-tranche buying ladder with explicit price levels and a total allocation cap; price declines must not trigger unlimited purchases.
- [ ] **Valuation and dividend sustainability.** Research payout ratios, earnings, cash flow and dividend history. Evaluate 2.5%-4.5% yields as a candidate valuation screen, and yields of 6% or more for ordinary companies as a review flag rather than proof of distress or value. Handle REITs and MLPs separately and account for market conditions.
- [ ] **Holding and exit rules.** Keep winners and avoid exits solely because prices fall or technical signals turn bearish. Detect confirmed cuts and omissions without confusing payment-frequency changes, special dividends, splits or ETF distribution variability with a company cutting its dividend. Specify the next-trading-session exit and clarify how the previous day's low sets the order, including gaps and unfilled orders. Define ETF exits separately from individual-company exits.
- [ ] **Portfolio and execution support.** Support repeated purchases, cost basis, dividend cash flows and optional reinvestment. Design long-term holding support around the existing one-active-trade-per-symbol rule, required stops, trailing stops, profit targets and time exits; define strategy-specific behavior while preserving account exposure limits and manual pause, close and flatten controls.
- [ ] **Validation and reporting.** Backtest with historical membership, dividend events, corporate actions, fees and taxes where applicable. Include dividend cuts, deep bear-market drawdowns and cash constraints; prevent survivorship bias and dividend double-counting with adjusted prices. Report dividend income, yield on cost, total return, drawdown and reinvestment separately, then validate on paper.

### Covered calls

Research an income strategy for owned shares in sideways or modestly rising markets. Avoid writing calls on positions selected for sharp appreciation, since the call caps upside while leaving substantial stock downside.

- [ ] **Selection and contract rules.** Define a measurable sideways-market filter and exclusions for strong momentum and upcoming catalysts. Choose strike, actual expiration date, minimum premium after costs, option liquidity and coverage ratio. Reserve enough owned shares for every sold call using the contract's actual deliverable; standard equity contracts generally represent 100 shares. Prevent selling reserved shares while leaving the call uncovered.
- [ ] **Outcomes and assignment.** Model three expiration price cases: below strike (call generally expires, shares remain), near/at strike (assignment is uncertain), and above strike (expect assignment and sale of shares at strike). Calculate combined stock-and-option P&L including premium, stock cost basis and fees. Use the contract calendar rather than assuming every expiration is Friday, and handle early assignment, ex-dividend exposure and after-hours exercise decisions.
- [ ] **Lifecycle and portfolio ownership.** Define buy-to-close, roll, assignment and stock-exit behavior, with linked stock and option positions, broker reconciliation and restart recovery. Keep ownership explicit when shares also belong to dividend or momentum strategies; selling a call must account for the possibility that the shares will be called away.
- [ ] **Dependencies and validation.** Reuse the options feed, contract models and execution support planned for iron condors. Add coverage checks, assignment events and reporting of net premium, total return and capped upside. Backtest sideways, rally and selloff periods with realistic spreads and fees, then validate on paper.

Reference: [OIC covered calls](https://www.optionseducation.org/strategies/all-strategies/covered-call-buy-write) and [assignment mechanics](https://www.optionseducation.org/referencelibrary/faq/options-assignment).

### Momentum stocks: separate long and short strategies

Define momentum as sustained directional price movement over a specified period. Research rapid revenue growth and young technology or other growth companies as optional selection factors; company age or industry alone does not establish momentum.

- [ ] **Long momentum.** Define lookback, trend strength, relative strength, liquidity and volume thresholds; specify buy entries, initial stops, trailing exits, failed-trend exits and maximum holding time. Compare with existing `stock_trend` and `trend_pullback` behavior before deciding what to reuse.
- [ ] **Short momentum.** Specify whether entries follow sustained downward momentum or a confirmed breakdown after an upward run. Define sell-to-open and buy-to-cover rules, stops above entry, trailing exits, squeeze/gap handling and maximum holding time. Do not short solely because a stock has risen sharply or has a high valuation.
- [ ] **Short-selling infrastructure.** Implement directional risk sizing, P&L, position tracking and reconciliation before enabling short entries; short selling is currently unsupported by the risk manager. Account for borrow availability, borrow fees, recalls, margin, dividend obligations and broker restrictions in execution and simulation.
- [ ] **Validation.** Backtest long and short strategies separately across market regimes, using information available at each decision date and realistic costs. Report hit rate, realized average win/loss, expectancy, total return and drawdown; validate both entry and exit rules on paper.

### Rocket stocks: high-volume breakout strategy

Research a long momentum variant targeting rapid appreciation over weeks or months. Treat the proposed breakout rules as hypotheses to validate rather than a promise of wealth.

- [ ] **Price and trend rules.** Require a breakout to a new 52-week high and a new all-time high, with price above the 50-day moving average and the 50-day moving average above the 200-day moving average. Define intraday versus completed-bar confirmation, sufficient price history and split-adjusted comparisons; calculate prior highs without including future data.
- [ ] **Volume confirmation and gaps.** Require a configurable surge relative to prior average volume. Flag gaps to new highs with unusually high volume for evaluation; define entry timing, maximum gap/extension and slippage limits rather than buying every alert. Compare intraday volume at equivalent times of day if using live partial bars.
- [ ] **Valuation and exits.** Evaluate the proposed approach without a low-P/E entry requirement; test whether valuation adds useful information rather than assuming it never matters. Define initial stop, position size, trailing exit, failed-breakout exit and a weeks-to-months holding limit, including overnight and earnings gaps.
- [ ] **Validation.** Compare with general long momentum and existing trend strategies. Include failed breakouts, delisted stocks, fees and slippage; use out-of-sample testing and paper validation before enabling execution.

### Put income strategies: covered puts and cash-secured puts

- [ ] **Covered put research.** Evaluate short stock plus a sold put as the bearish counterpart to a covered call. Specify share coverage, strike, expiration, net premium, borrow costs and assignment-driven purchase of shares to close the short. Model limited gains and potentially unlimited loss if the stock rises; this depends on both options support and the short-selling infrastructure above.
- [ ] **Cash-secured put research.** Evaluate selling a put while reserving enough cash to buy the shares at the strike if assigned. Define eligible stocks the portfolio is willing to own, strike/expiration selection, cash reservation, assignment and subsequent holding or exit rules. This is a stock-acquisition/income sibling of covered calls, distinct from a covered put backed by short stock; account for substantial losses if the stock falls.
- [ ] **Validation and reporting.** Model early assignment, exercise/expiration, collateral changes, commissions, spreads and portfolio exposure for each strategy separately. Compare total return and drawdown with covered calls and stock ownership, and validate on paper.

References: [OIC covered puts](https://www.optionseducation.org/strategies/all-strategies/covered-put) and [cash-secured puts](https://www.optionseducation.org/strategies/all-strategies/cash-secured-put).
