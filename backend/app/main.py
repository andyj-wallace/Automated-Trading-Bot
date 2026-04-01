from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.middleware import ErrorHandlerMiddleware
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _position_monitor

    setup_logging(log_level=settings.log_level)
    system_logger.info(
        "Application starting",
        extra={"environment": settings.environment, "log_level": settings.log_level},
    )

    # ------------------------------------------------------------------
    # Start PositionMonitor as a background task
    # ------------------------------------------------------------------
    from app.core.execution.order_manager import OrderManager
    from app.core.execution.position_monitor import PositionMonitor
    from app.core.risk.manager import RiskManager
    from app.data.cache import RedisCache
    from app.db.session import AsyncSessionFactory
    from app.dependencies import _build_broker

    broker = _build_broker()
    cache = RedisCache(settings.redis_url)
    order_manager = OrderManager(
        broker=broker,
        risk_manager=RiskManager(),
        session_factory=AsyncSessionFactory,
        cache=cache,
    )
    _position_monitor = PositionMonitor(
        cache=cache,
        session_factory=AsyncSessionFactory,
        order_manager=order_manager,
    )
    await _position_monitor.start()

    yield

    # ------------------------------------------------------------------
    # Graceful shutdown
    # ------------------------------------------------------------------
    if _position_monitor:
        await _position_monitor.stop()
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

# WebSocket — no version prefix, path is /ws/dashboard
app.include_router(ws_router)


@app.get("/", tags=["root"])
async def root():
    return {"status": "ok", "environment": settings.environment}


@app.get("/health", tags=["root"])
async def health():
    return {"status": "ok"}
