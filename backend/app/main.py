import asyncio
from contextlib import asynccontextmanager
from decimal import Decimal

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.middleware import ErrorHandlerMiddleware
from app.api.v1.backtesting import router as backtesting_router
from app.api.v1.metrics import router as metrics_router
from app.api.v1.portfolio import router as portfolio_router
from app.api.v1.strategies import router as strategies_router
from app.api.v1.symbols import router as symbols_router
from app.api.v1.system import router as system_router
from app.api.v1.trades import router as trades_router
from app.api.websocket import router as ws_router
from app.config import get_settings
from app.monitoring.logger import setup_logging, system_logger

settings = get_settings()

_API_PREFIX = "/api/v1"

_position_monitor = None
_strategy_scheduler = None
_risk_monitor_task = None
_notification_task = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _position_monitor, _strategy_scheduler, _risk_monitor_task, _notification_task

    setup_logging(log_level=settings.log_level)
    system_logger.info(
        "Application starting",
        extra={"environment": settings.environment, "log_level": settings.log_level},
    )

    import app.core.strategy_engine.moving_average  # noqa: F401 — triggers self-registration

    from app.core.execution.order_manager import OrderManager
    from app.core.execution.position_monitor import PositionMonitor
    from app.core.risk.manager import RiskManager
    from app.core.strategy_engine.registry import registry
    from app.core.strategy_engine.scheduler import StrategyScheduler
    from app.data.cache import RedisCache
    from app.db.session import AsyncSessionFactory
    from app.dependencies import _build_broker

    broker = _build_broker()
    cache = RedisCache(settings.redis_url)
    risk_manager = RiskManager()
    order_manager = OrderManager(
        broker=broker,
        risk_manager=risk_manager,
        session_factory=AsyncSessionFactory,
        cache=cache,
    )

    # ------------------------------------------------------------------
    # Start PositionMonitor
    # ------------------------------------------------------------------
    _position_monitor = PositionMonitor(
        cache=cache,
        session_factory=AsyncSessionFactory,
        order_manager=order_manager,
    )
    await _position_monitor.start()

    # ------------------------------------------------------------------
    # Start StrategyScheduler
    # ------------------------------------------------------------------
    _strategy_scheduler = StrategyScheduler(
        registry=registry,
        broker=broker,
        risk_manager=risk_manager,
        cache=cache,
        session_factory=AsyncSessionFactory,
        order_manager=order_manager,
    )
    await _strategy_scheduler.start()

    # ------------------------------------------------------------------
    # Start RiskMonitor background loop (Layer 14.1)
    # Publishes WARNING/CRITICAL alerts to Redis `risk_updates` channel
    # so the WebSocket endpoint can push them to the dashboard.
    # ------------------------------------------------------------------
    from app.core.risk.monitor import RiskMonitor

    _risk_monitor = RiskMonitor(cache=cache)

    async def _get_balance() -> Decimal:
        if broker.is_connected():
            try:
                summary = await broker.get_account_summary()
                return summary.net_liquidation
            except Exception:
                pass
        return Decimal("0")

    _risk_monitor_task = asyncio.create_task(
        _risk_monitor.run_loop(AsyncSessionFactory, _get_balance),
        name="risk_monitor",
    )

    # ------------------------------------------------------------------
    # Start NotificationDispatcher subscriber (Layer 16.5)
    # Listens on risk_updates + trade_events channels and emails alerts.
    # ------------------------------------------------------------------
    from app.notifications.dispatcher import NotificationDispatcher

    _dispatcher = NotificationDispatcher(settings)

    async def _notification_loop() -> None:
        import json
        try:
            async for msg in cache.subscribe_many(
                channels=["risk_updates", "trade_events"],
                patterns=[],
            ):
                try:
                    event = json.loads(msg["data"])
                    await _dispatcher.dispatch(event)
                except Exception as exc:
                    system_logger.warning(
                        "NotificationDispatcher: failed to process event",
                        extra={"error": str(exc)},
                    )
        except asyncio.CancelledError:
            pass

    _notification_task = asyncio.create_task(
        _notification_loop(),
        name="notification_dispatcher",
    )

    yield

    # ------------------------------------------------------------------
    # Graceful shutdown
    # ------------------------------------------------------------------
    if _strategy_scheduler:
        await _strategy_scheduler.stop()
    if _position_monitor:
        await _position_monitor.stop()
    if _risk_monitor_task and not _risk_monitor_task.done():
        _risk_monitor.stop()
        _risk_monitor_task.cancel()
        try:
            await _risk_monitor_task
        except asyncio.CancelledError:
            pass
    if _notification_task and not _notification_task.done():
        _notification_task.cancel()
        try:
            await _notification_task
        except asyncio.CancelledError:
            pass
    await cache.close()
    system_logger.info("Application shutting down")


app = FastAPI(
    title="Automated Trading Bot",
    description="Personal algorithmic trading bot API",
    version="0.1.0",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

# Middleware — ErrorHandlerMiddleware must be added BEFORE CORSMiddleware
# so that error responses also get CORS headers.
app.add_middleware(ErrorHandlerMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],  # Vite dev server
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# REST API v1 routers
app.include_router(symbols_router, prefix=_API_PREFIX)
app.include_router(trades_router, prefix=_API_PREFIX)
app.include_router(strategies_router, prefix=_API_PREFIX)
app.include_router(portfolio_router, prefix=_API_PREFIX)
app.include_router(system_router, prefix=_API_PREFIX)
app.include_router(backtesting_router, prefix=_API_PREFIX)
app.include_router(metrics_router, prefix=_API_PREFIX)

# WebSocket — no version prefix, path is /ws/dashboard
app.include_router(ws_router)


@app.get("/", tags=["root"])
async def root():
    return {"status": "ok", "environment": settings.environment}


@app.get("/health", tags=["root"])
async def health():
    return {"status": "ok"}
