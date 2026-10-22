#!/usr/bin/env sh
# Run the trading bot's production stack (docker-compose.prod.yml + .env.prod). See DEPLOYMENT.md.
set -eu
cd "$(dirname "$0")/.."

usage() {
  cat <<'EOF'
Usage: scripts/bot.sh <command>

  up               Build and start everything (IB Gateway, database, Redis, backend, frontend)
  down             Stop everything (data is kept)
  status           Show the containers and the broker connection
  logs [service]   Follow logs: all, or one of ib-gateway, backend, frontend, db, redis
  restart-gateway  Restart IB Gateway (approve the login in IBKR Mobile if it asks)
EOF
}

compose() {
  if [ ! -f .env.prod ]; then
    echo "Missing .env.prod: copy .env.prod.example to .env.prod and fill it in." >&2
    exit 1
  fi
  docker compose -f docker-compose.prod.yml --env-file .env.prod "$@"
}

STATUS_PY='
import json, urllib.error, urllib.request
try:
    with urllib.request.urlopen("http://127.0.0.1:8000/api/v1/trading/status", timeout=5) as r:
        d = json.load(r)["data"]
except urllib.error.HTTPError as e:
    raise SystemExit(f"Backend answered HTTP {e.code} (503 = trading engine not running). Check: scripts/bot.sh logs backend")
except Exception as e:
    raise SystemExit(f"Backend not answering ({e}). Check: scripts/bot.sh logs backend")
b = d["broker"]
print("Broker:      ", b["state"])
if b.get("hint"):
    print("What to do:  ", b["hint"])
print("Paused:      ", d["paused"])
print("Market open: ", d["market_open"])
print("Open trades: ", d["open_trade_count"])
'

case "${1:-}" in
  up) compose up -d --build ;;
  down) compose down ;;
  status)
    compose ps
    echo
    compose exec -T backend python -c "$STATUS_PY"
    ;;
  logs)
    shift
    compose logs -f --tail 200 "$@"
    ;;
  restart-gateway) compose restart ib-gateway ;;
  "" | -h | --help | help) usage ;;
  *)
    usage >&2
    exit 1
    ;;
esac
