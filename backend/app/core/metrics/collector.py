"""
MetricsCollector — writes KPI snapshots to the portfolio_snapshots TimescaleDB
hypertable on trade events (open and close).

Called by OrderManager after a fill is confirmed and after a position closes.
Each call takes a point-in-time snapshot of aggregate risk and equity, then
writes one PortfolioSnapshot row.

Depends on: PortfolioRepo (3.9), TradeRepo (3.7), BaseBroker (4.1)
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.portfolio import PortfolioSnapshot
from app.db.repositories.portfolio_repo import PortfolioRepo
from app.db.repositories.trade_repo import TradeRepo
from app.monitoring.logger import system_logger


class MetricsCollector:
    """
    Captures and persists a portfolio KPI snapshot.

    Usage:
        collector = MetricsCollector()
        await collector.record_snapshot(session, broker)
    """

    async def record_snapshot(
        self,
        session: AsyncSession,
        broker,  # BaseBroker — typed loosely to avoid circular import
    ) -> PortfolioSnapshot | None:
        """
        Query open trades + broker account summary and write one snapshot row.

        Returns the persisted snapshot, or None if the broker is unreachable
        (non-fatal — the caller should continue without a snapshot).
        """
        # Fetch account data from broker
        total_equity = Decimal("0")
        cash_balance = Decimal("0")
        gross_position_value = Decimal("0")

        if broker.is_connected():
            try:
                summary = await broker.get_account_summary()
                total_equity = summary.net_liquidation
                cash_balance = summary.cash_balance
                gross_position_value = summary.gross_position_value
            except Exception as exc:
                system_logger.warning(
                    "MetricsCollector: broker unavailable — snapshot will use zeroed equity",
                    extra={"error": str(exc)},
                )

        # Compute aggregate risk from open trades
        trade_repo = TradeRepo(session)
        open_trades = await trade_repo.get_open_trades()

        aggregate_risk = sum(
            (t.risk_amount for t in open_trades), Decimal("0")
        )
        aggregate_risk_pct = (
            aggregate_risk / total_equity if total_equity > 0 else Decimal("0")
        )

        # Max per-trade risk as % of equity
        max_per_trade = Decimal("0")
        if open_trades and total_equity > 0:
            max_per_trade = max(
                t.risk_amount / total_equity for t in open_trades
            )

        snapshot = PortfolioSnapshot(
            time=datetime.now(timezone.utc),
            total_equity=total_equity,
            cash_balance=cash_balance,
            open_position_value=gross_position_value,
            open_trade_count=len(open_trades),
            aggregate_risk_amount=aggregate_risk,
            aggregate_risk_pct=aggregate_risk_pct,
            max_per_trade_risk_pct=max_per_trade,
        )

        try:
            repo = PortfolioRepo(session)
            saved = await repo.insert_snapshot(snapshot)
            await session.commit()
            system_logger.info(
                "MetricsCollector: snapshot written",
                extra={
                    "open_trades": len(open_trades),
                    "aggregate_risk_pct": f"{aggregate_risk_pct:.4%}",
                    "total_equity": str(total_equity),
                },
            )
            return saved
        except Exception as exc:
            system_logger.error(
                "MetricsCollector: failed to write snapshot",
                extra={"error": str(exc)},
            )
            return None
