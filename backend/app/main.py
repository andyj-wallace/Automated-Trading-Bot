from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.middleware import ErrorHandlerMiddleware
from app.api.v1.backtesting import router as backtesting_router
from app.api.v1.metrics import router as metrics_router
from app.api.v1.portfolio import router as portfolio_router
from app.api.v1.risk import router as risk_router
from app.api.v1.signals import router as signals_router
from app.api.v1.strategies import router as strategies_router
from app.api.v1.symbols import router as symbols_router
from app.api.v1.system import router as system_router
from app.api.v1.trades import router as trades_router
from app.api.v1.trading import router as trading_router
from app.api.websocket import router as ws_router
from app.config import get_settings
from app.core.strategy_engine.builtin import load_builtin_strategies
from app.monitoring.logger import setup_logging, system_logger

settings = get_settings()

load_builtin_strategies()

_API_PREFIX = "/api/v1"

# ---------------------------------------------------------------------------
# Security audit — runs at startup, logs findings to system.log
# ---------------------------------------------------------------------------

_WEAK_SECRET_PATTERNS = {"", "change-me", "changeme", "secret", "secret-key", "dev", "development"}


def _run_security_audit(cfg) -> None:
    """
    Log security posture at startup. Emits WARNING for each issue found.
    Never blocks startup — awareness only.
    """
    issues: list[str] = []

    # Secret key must be non-empty and not a known weak default
    sk = (cfg.secret_key or "").lower().strip()
    if sk in _WEAK_SECRET_PATTERNS or len(sk) < 32:
        issues.append(
            "SECRET_KEY is weak or too short — generate with: "
            "python -c \"import secrets; print(secrets.token_hex(32))\""
        )

    # In production: API host should be localhost-only
    if cfg.is_production and cfg.api_host not in ("127.0.0.1", "localhost"):
        issues.append(
            f"API_HOST is '{cfg.api_host}' — production APIs should bind to "
            "127.0.0.1 (localhost) or sit behind a reverse proxy"
        )

    # In production: SMTP should use TLS (smtps://)
    smtp = cfg.notification_email_smtp or ""
    if cfg.is_production and smtp and not smtp.startswith("smtps://"):
        issues.append(
            "NOTIFICATION_EMAIL_SMTP uses plain smtp:// in production — "
            "use smtps:// for TLS-encrypted delivery"
        )

    # In production: paper trading mode should not be used with live broker
    if cfg.is_production and cfg.broker == "mock":
        issues.append(
            "BROKER=mock in production — live trading will not execute real orders"
        )

    if issues:
        for issue in issues:
            system_logger.warning(f"[SECURITY AUDIT] {issue}")
    else:
        system_logger.info("[SECURITY AUDIT] All checks passed")

@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging(log_level=settings.log_level, log_dir=settings.log_dir)
    system_logger.info(
        "Application starting",
        extra={"environment": settings.environment, "log_level": settings.log_level},
    )
    _run_security_audit(settings)

    engine = None
    if settings.engine_enabled:
        from app.core.engine import TradingEngine

        engine = TradingEngine(settings)
        await engine.start()
        app.state.engine = engine
    else:
        system_logger.warning("ENGINE_ENABLED=false: trading engine not started")

    yield

    if engine is not None:
        await engine.stop()
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
    allow_origins=settings.cors_origins,
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
app.include_router(risk_router, prefix=_API_PREFIX)
app.include_router(trading_router, prefix=_API_PREFIX)
app.include_router(signals_router, prefix=_API_PREFIX)

# WebSocket — no version prefix, path is /ws/dashboard
app.include_router(ws_router)


@app.get("/", tags=["root"])
async def root():
    return {"status": "ok", "environment": settings.environment}


@app.get("/health", tags=["root"])
async def health():
    return {"status": "ok"}
