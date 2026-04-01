"""
RiskMonitor — tracks aggregate open exposure and emits log-based alerts.

Polls open trades via TradeRepo, computes the total risk as a percentage of
account balance, and logs WARNING / CRITICAL entries when configurable
thresholds are crossed.

Alert thresholds (relative to max_aggregate_risk_pct):
  WARNING  → aggregate exposure ≥ 75% of max  (default: 3.75%)
  CRITICAL → aggregate exposure ≥ 90% of max  (default: 4.50%)

Redis pub/sub wiring is added in Layer 14. For now all alerts are log entries.

Depends on: TradeRepo (Layer 3.7).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.trade_repo import TradeRepo
from app.monitoring.logger import risk_logger

# Hard ceiling: aggregate portfolio risk must never exceed 10% of account balance.
# This constant is shared with RiskManager (which has its own copy) and enforced
# here so RiskMonitorConfig cannot be configured beyond this value.
MAX_PORTFOLIO_RISK_HARD_LIMIT = Decimal("0.10")

# Default maximum aggregate risk: 5% = up to 5 concurrent trades × 1% each.
DEFAULT_MAX_AGGREGATE_RISK_PCT = Decimal("0.05")

# Alert thresholds as fractions of the configured maximum.
_WARNING_FRACTION = Decimal("0.75")
_CRITICAL_FRACTION = Decimal("0.90")

# Default polling interval when running as a background loop.
DEFAULT_POLL_INTERVAL_SECONDS = 30


@dataclass
class RiskMonitorConfig:
    """Configurable thresholds for RiskMonitor."""

    max_aggregate_risk_pct: Decimal = field(default=DEFAULT_MAX_AGGREGATE_RISK_PCT)
    warning_fraction: Decimal = field(default=_WARNING_FRACTION)
    critical_fraction: Decimal = field(default=_CRITICAL_FRACTION)
    poll_interval_seconds: int = field(default=DEFAULT_POLL_INTERVAL_SECONDS)

    def __post_init__(self) -> None:
        if self.max_aggregate_risk_pct > MAX_PORTFOLIO_RISK_HARD_LIMIT:
            raise ValueError(
                f"max_aggregate_risk_pct {self.max_aggregate_risk_pct} exceeds "
                f"hard limit {MAX_PORTFOLIO_RISK_HARD_LIMIT}. "
                "The portfolio risk ceiling is non-negotiable."
            )

    @property
    def warning_threshold(self) -> Decimal:
        return self.max_aggregate_risk_pct * self.warning_fraction

    @property
    def critical_threshold(self) -> Decimal:
        return self.max_aggregate_risk_pct * self.critical_fraction


@dataclass
class ExposureStatus:
    """Result of a single exposure check."""

    aggregate_risk_amount: Decimal
    aggregate_risk_pct: Decimal
    open_trade_count: int
    account_balance: Decimal
    alert_level: str  # "NONE", "WARNING", "CRITICAL"


class RiskMonitor:
    """
    Monitors aggregate open-trade exposure and emits threshold alerts.

    Usage (one-off check):
        monitor = RiskMonitor()
        status = await monitor.check_exposure(session, account_balance=Decimal("100000"))

    Usage (background loop):
        await monitor.run_loop(session_factory, get_account_balance)
    """

    def __init__(self, config: RiskMonitorConfig | None = None) -> None:
        self._config = config or RiskMonitorConfig()
        self._running = False

    # ------------------------------------------------------------------
    # Single exposure check
    # ------------------------------------------------------------------

    async def check_exposure(
        self,
        session: AsyncSession,
        account_balance: Decimal,
    ) -> ExposureStatus:
        """
        Compute aggregate open exposure and emit alerts if thresholds are crossed.

        Args:
            session:         Active DB session (read-only access to trades table).
            account_balance: Current account equity from the broker.

        Returns:
            ExposureStatus with current aggregate risk and the alert level fired.
        """
        repo = TradeRepo(session)
        open_trades = await repo.get_open_trades()

        aggregate_risk = sum(
            (t.risk_amount for t in open_trades), Decimal("0")
        )
        aggregate_pct = (
            aggregate_risk / account_balance if account_balance > 0 else Decimal("0")
        )
        alert_level = self._evaluate_alert(aggregate_pct)

        status = ExposureStatus(
            aggregate_risk_amount=aggregate_risk,
            aggregate_risk_pct=aggregate_pct,
            open_trade_count=len(open_trades),
            account_balance=account_balance,
            alert_level=alert_level,
        )

        self._log_status(status)
        return status

    # ------------------------------------------------------------------
    # Background polling loop
    # ------------------------------------------------------------------

    async def run_loop(
        self,
        session_factory,
        get_account_balance,
    ) -> None:
        """
        Run check_exposure() on a recurring interval until stop() is called.

        Args:
            session_factory:     Callable that returns an async context-manager
                                 yielding an AsyncSession (e.g. AsyncSessionFactory).
            get_account_balance: Async callable → Decimal; fetches current balance
                                 from the broker (e.g. broker.get_account_summary()).
        """
        self._running = True
        while self._running:
            try:
                balance = await get_account_balance()
                async with session_factory() as session:
                    await self.check_exposure(session, balance)
            except Exception as exc:
                risk_logger.error(
                    "RiskMonitor loop error",
                    extra={"error": str(exc)},
                )
            await asyncio.sleep(self._config.poll_interval_seconds)

    def stop(self) -> None:
        """Signal the background loop to exit after the current sleep."""
        self._running = False

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _evaluate_alert(self, aggregate_pct: Decimal) -> str:
        if aggregate_pct >= self._config.critical_threshold:
            return "CRITICAL"
        if aggregate_pct >= self._config.warning_threshold:
            return "WARNING"
        return "NONE"

    def _log_status(self, status: ExposureStatus) -> None:
        extra = {
            "aggregate_risk_amount": str(status.aggregate_risk_amount),
            "aggregate_risk_pct": f"{status.aggregate_risk_pct:.4%}",
            "open_trade_count": status.open_trade_count,
            "account_balance": str(status.account_balance),
            "warning_threshold": f"{self._config.warning_threshold:.4%}",
            "critical_threshold": f"{self._config.critical_threshold:.4%}",
        }

        if status.alert_level == "CRITICAL":
            risk_logger.critical(
                "RISK MONITOR: aggregate exposure at CRITICAL level",
                extra=extra,
            )
        elif status.alert_level == "WARNING":
            risk_logger.warning(
                "RISK MONITOR: aggregate exposure at WARNING level",
                extra=extra,
            )
        else:
            risk_logger.info(
                "RISK MONITOR: exposure check — within limits",
                extra=extra,
            )
