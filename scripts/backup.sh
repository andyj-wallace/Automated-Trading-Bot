#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")/.."
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
mkdir -p backups
docker compose -f docker-compose.prod.yml exec -T db pg_dump -U trading -d trading_bot -Fc > "backups/trading_bot_${STAMP}.dump"
docker compose -f docker-compose.prod.yml cp backend:/app/logs/audit.log "backups/audit_${STAMP}.log"
find backups -type f -mtime +14 -delete
