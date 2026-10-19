"""
System health endpoint.

GET /api/v1/system/health — Check broker, database, Redis and trading engine.
                            Returns HTTP 200 if all components are healthy,
                            HTTP 503 if any critical component is down.
                            Total response time target: < 500ms.
"""

import asyncio
import time
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.schemas import ComponentStatus, SystemHealthOut, ok
from app.brokers.base import BaseBroker
from app.data.cache import RedisCache
from app.dependencies import get_broker, get_cache, get_db
from app.monitoring.logger import system_logger

router = APIRouter(prefix="/system", tags=["system"])

_COMPONENT_TIMEOUT = 0.4  # seconds per async check; keeps total well under 500ms


async def _engine_status(engine) -> ComponentStatus:
    """ok when the engine is started and holds the lock; info explains whether it is trading."""
    if engine is None or not engine.started:
        return ComponentStatus(status="error", detail="Trading engine is not running")
    now = datetime.now(UTC)
    ages = []
    for symbol in engine.position_monitor.tracked_symbols():
        stamp = engine.feed.last_tick_at(symbol)
        if stamp is not None:
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=UTC)
            ages.append((now - stamp).total_seconds())
    last_cycle = engine.scheduler.last_cycle_at
    info = {
        "feed_subscriptions": len(engine.feed.subscribed_tickers),
        "max_tick_age_seconds": round(max(ages), 1) if ages else None,
        "scheduler_last_cycle_at": last_cycle.isoformat() if last_cycle else None,
        "tracked_trades": engine.position_monitor.tracked_count,
        "paused": await engine.trading_state.is_paused(),
        "market_open": engine.market_open(),
    }
    if not engine.is_leader:
        return ComponentStatus(status="error", detail="Engine lock not held", info=info)
    return ComponentStatus(status="ok", info=info)


@router.get("/health")
async def system_health(
    request: Request,
    db: AsyncSession = Depends(get_db),
    broker: BaseBroker = Depends(get_broker),
    cache: RedisCache = Depends(get_cache),
) -> JSONResponse:
    """
    Aggregate health of all critical components.

    - broker:   synchronous is_connected() check — no I/O
    - database: SELECT 1 with a 400ms timeout
    - redis:    PING with a 400ms timeout
    - engine:   started and holding the engine lock; info shows feed/scheduler state

    DB and Redis checks run concurrently via asyncio.gather.
    """
    request_start = time.monotonic()

    # Broker — synchronous; no I/O required
    broker_connected = broker.is_connected()
    broker_status = ComponentStatus(status="ok" if broker_connected else "disconnected")
    system_logger.debug(
        "Health check: broker",
        extra={"status": broker_status.status, "broker_type": type(broker).__name__},
    )

    async def check_db() -> ComponentStatus:
        t = time.monotonic()
        try:
            await asyncio.wait_for(
                db.execute(text("SELECT 1")),
                timeout=_COMPONENT_TIMEOUT,
            )
            elapsed_ms = round((time.monotonic() - t) * 1000)
            system_logger.debug("Health check: database", extra={"status": "ok", "elapsed_ms": elapsed_ms})
            return ComponentStatus(status="ok")
        except Exception as exc:
            elapsed_ms = round((time.monotonic() - t) * 1000)
            system_logger.warning(
                "Health check: database failed",
                extra={"status": "error", "elapsed_ms": elapsed_ms, "error": str(exc)},
            )
            return ComponentStatus(status="error", detail=str(exc))

    async def check_redis() -> ComponentStatus:
        t = time.monotonic()
        try:
            reachable = await asyncio.wait_for(cache.ping(), timeout=_COMPONENT_TIMEOUT)
            status = "ok" if reachable else "error"
            elapsed_ms = round((time.monotonic() - t) * 1000)
            system_logger.debug("Health check: redis", extra={"status": status, "elapsed_ms": elapsed_ms})
            return ComponentStatus(status=status)
        except Exception as exc:
            elapsed_ms = round((time.monotonic() - t) * 1000)
            system_logger.warning(
                "Health check: redis failed",
                extra={"status": "error", "elapsed_ms": elapsed_ms, "error": str(exc)},
            )
            return ComponentStatus(status="error", detail=str(exc))

    db_status, redis_status = await asyncio.gather(check_db(), check_redis())
    engine_status = await _engine_status(getattr(request.app.state, "engine", None))

    all_healthy = all(
        s.status == "ok" for s in [broker_status, db_status, redis_status, engine_status]
    )
    overall = "ok" if all_healthy else "degraded"
    total_ms = round((time.monotonic() - request_start) * 1000)

    system_logger.debug(
        "Health check: complete",
        extra={
            "overall": overall,
            "broker": broker_status.status,
            "database": db_status.status,
            "redis": redis_status.status,
            "engine": engine_status.status,
            "total_ms": total_ms,
        },
    )

    health = SystemHealthOut(
        status=overall,
        broker=broker_status,
        database=db_status,
        redis=redis_status,
        engine=engine_status,
    )

    http_status = 200 if all_healthy else 503
    return JSONResponse(status_code=http_status, content=ok(health.model_dump()))
