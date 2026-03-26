"""
System health endpoint.

GET /api/v1/system/health — Check broker, database, and Redis connectivity.
                            Returns HTTP 200 if all components are healthy,
                            HTTP 503 if any critical component is down.
                            Total response time target: < 500ms.
"""

import asyncio

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.schemas import ComponentStatus, SystemHealthOut, ok
from app.brokers.base import BaseBroker
from app.data.cache import RedisCache
from app.dependencies import get_broker, get_cache, get_db

router = APIRouter(prefix="/system", tags=["system"])

_COMPONENT_TIMEOUT = 0.4  # seconds per async check; keeps total well under 500ms


@router.get("/health")
async def system_health(
    db: AsyncSession = Depends(get_db),
    broker: BaseBroker = Depends(get_broker),
    cache: RedisCache = Depends(get_cache),
) -> JSONResponse:
    """
    Aggregate health of all critical components.

    - broker:   synchronous is_connected() check — no I/O
    - database: SELECT 1 with a 400ms timeout
    - redis:    PING with a 400ms timeout

    DB and Redis checks run concurrently via asyncio.gather.
    """

    # Broker — synchronous; no I/O required
    broker_status = ComponentStatus(
        status="ok" if broker.is_connected() else "disconnected"
    )

    async def check_db() -> ComponentStatus:
        try:
            await asyncio.wait_for(
                db.execute(text("SELECT 1")),
                timeout=_COMPONENT_TIMEOUT,
            )
            return ComponentStatus(status="ok")
        except Exception as exc:
            return ComponentStatus(status="error", detail=str(exc))

    async def check_redis() -> ComponentStatus:
        try:
            reachable = await asyncio.wait_for(cache.ping(), timeout=_COMPONENT_TIMEOUT)
            return ComponentStatus(status="ok" if reachable else "error")
        except Exception as exc:
            return ComponentStatus(status="error", detail=str(exc))

    db_status, redis_status = await asyncio.gather(check_db(), check_redis())

    all_healthy = all(
        s.status == "ok" for s in [broker_status, db_status, redis_status]
    )
    overall = "ok" if all_healthy else "degraded"

    health = SystemHealthOut(
        status=overall,
        broker=broker_status,
        database=db_status,
        redis=redis_status,
    )

    http_status = 200 if all_healthy else 503
    return JSONResponse(status_code=http_status, content=ok(health.model_dump()))
