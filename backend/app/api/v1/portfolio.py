"""
Portfolio risk endpoint.

GET /api/v1/portfolio/risk — Current aggregate risk exposure across all open trades
"""

from decimal import Decimal

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.schemas import ok
from app.brokers.base import BaseBroker
from app.core.risk.monitor import RiskMonitor, RiskMonitorConfig
from app.dependencies import get_broker, get_db
from app.monitoring.logger import system_logger

router = APIRouter(prefix="/portfolio", tags=["portfolio"])


@router.get("/risk")
async def get_portfolio_risk(
    db: AsyncSession = Depends(get_db),
    broker: BaseBroker = Depends(get_broker),
) -> JSONResponse:
    """
    Return live aggregate risk exposure.

    Queries open trades from the DB and computes total risk as a percentage
    of the current account balance. If the broker is unreachable, the balance
    defaults to 0 and all percentage fields will be 0.
    """
    balance = Decimal("0")
    if broker.is_connected():
        try:
            summary = await broker.get_account_summary()
            balance = summary.net_liquidation
        except Exception as exc:
            system_logger.warning(
                "Could not fetch account balance for risk endpoint",
                extra={"error": str(exc)},
            )

    config = RiskMonitorConfig()
    monitor = RiskMonitor(config=config)
    status = await monitor.check_exposure(db, balance)

    data = {
        "aggregate_risk_amount": str(status.aggregate_risk_amount),
        "aggregate_risk_pct": str(status.aggregate_risk_pct),
        "open_trade_count": status.open_trade_count,
        "account_balance": str(balance),
        "alert_level": status.alert_level,
        "warning_threshold_pct": str(config.warning_threshold),
        "critical_threshold_pct": str(config.critical_threshold),
    }

    return JSONResponse(content=ok(data))
