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


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging(log_level=settings.log_level)
    system_logger.info(
        "Application starting",
        extra={"environment": settings.environment, "log_level": settings.log_level},
    )
    yield
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
