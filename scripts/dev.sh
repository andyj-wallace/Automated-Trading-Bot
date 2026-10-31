#!/usr/bin/env sh
# Run the bot on this Mac: IB Gateway, Postgres and Redis in Docker; backend and frontend as local processes.
#   scripts/dev.sh [up]     start everything (BROKER=mock scripts/dev.sh skips the Gateway)
#   scripts/dev.sh down     stop everything
#   scripts/dev.sh status   what is running, and the broker connection
# Logs and PIDs: logs/dev-backend.log, logs/dev-frontend.log
set -eu
cd "$(dirname "$0")/.."
ROOT=$(pwd)
LOGS="$ROOT/logs"
PYTHON=${PYTHON:-python3}
BROKER=${BROKER:-ibkr}
BACKEND_URL=http://127.0.0.1:8000
FRONTEND_URL=http://127.0.0.1:5173

answers() { curl -s -o /dev/null --max-time 2 "$1"; }

# start_bg <name> <dir> <url> <command...>: run in the background, then wait until <url> answers.
start_bg() {
  name=$1 dir=$2 url=$3
  shift 3
  if answers "$url"; then
    echo "$name: already running"
    return
  fi
  # Background the program itself (not a subshell) so the PID file holds its real PID.
  cd "$dir"
  nohup "$@" < /dev/null > "$LOGS/dev-$name.log" 2>&1 &
  echo $! > "$LOGS/dev-$name.pid"
  cd "$ROOT"
  i=0
  until answers "$url"; do
    i=$((i + 1))
    if [ "$i" -ge 60 ]; then
      echo "$name: did not start within 60s, see logs/dev-$name.log" >&2
      exit 1
    fi
    sleep 1
  done
  echo "$name: started"
}

STATUS_PY='
import json, sys
d = json.load(sys.stdin).get("data") or {}
b = d.get("broker") or {}
print("broker:   ", b.get("state", "unknown (trading engine not running)"))
if b.get("hint"):
    print("what to do:", b["hint"])
'

status() {
  docker compose --profile ibkr ps --format '{{.Service}}: {{.State}}'
  if answers "$FRONTEND_URL"; then echo "frontend: running  $FRONTEND_URL"; else echo "frontend: not running"; fi
  if answers "$BACKEND_URL/health"; then
    echo "backend:  running  $BACKEND_URL"
    curl -s "$BACKEND_URL/api/v1/trading/status" | "$PYTHON" -c "$STATUS_PY"
  else
    echo "backend:  not running"
  fi
}

up() {
  docker info > /dev/null 2>&1 || { echo "Docker is not running: start Docker Desktop first." >&2; exit 1; }
  mkdir -p "$LOGS"
  if [ "$BROKER" = ibkr ]; then
    docker compose --profile ibkr up -d --wait db redis ib-gateway
  else
    docker compose up -d --wait db redis
  fi
  (cd backend && "$PYTHON" -m alembic upgrade head)
  # The Gateway container publishes the paper API on 127.0.0.1:4002 (never the live ports).
  start_bg backend backend "$BACKEND_URL/health" \
    env BROKER="$BROKER" IBKR_HOST=127.0.0.1 IBKR_PORT=4002 IBKR_TRADING_MODE=paper \
    "$PYTHON" -m uvicorn app.main:app --host 127.0.0.1 --port 8000
  [ -d frontend/node_modules ] || (cd frontend && npm install)
  start_bg frontend frontend "$FRONTEND_URL" ./node_modules/.bin/vite --host 127.0.0.1 --strictPort
  echo
  status
  [ "$BROKER" = ibkr ] && echo "If IBKR Mobile asks you to approve a login, tap Approve; the backend connects by itself."
  echo "Open $FRONTEND_URL"
}

down() {
  for name in frontend backend; do
    pidfile="$LOGS/dev-$name.pid"
    if [ -f "$pidfile" ] && kill "$(cat "$pidfile")" 2> /dev/null; then
      echo "$name: stopped"
    else
      echo "$name: not running (or not started by this script)"
    fi
    rm -f "$pidfile"
  done
  docker compose --profile ibkr stop
}

case "${1:-up}" in
  up) up ;;
  down) down ;;
  status) status ;;
  *)
    echo "Usage: scripts/dev.sh [up|down|status]" >&2
    exit 1
    ;;
esac
