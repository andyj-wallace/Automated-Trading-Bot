"""DailySummaryJob: end-of-day portfolio snapshot and one summary email per trading day."""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Callable
from datetime import date, datetime, time
from decimal import Decimal

from app.core import market_calendar
from app.core.metrics.collector import MetricsCollector
from app.db.repositories.trade_repo import TradeRepo
from app.monitoring.logger import system_logger

LAST_RUN_KEY = "job:daily_summary:last_run"
_HALT_ALERTS = ("CIRCUIT_BREAKER_HALT", "STRATEGY_AUTO_DISABLED")


class DailySummaryJob:
    """Writes a PortfolioSnapshot, then emails the day's results once per trading day."""

    def __init__(
        self,
        broker,
        session_factory,
        cache,
        dispatcher,
        *,
        run_at: time,
        clock: Callable[[], datetime] = market_calendar.now_et,
        collector: MetricsCollector | None = None,
    ) -> None:
        self._broker = broker
        self._session_factory = session_factory
        self._cache = cache
        self._dispatcher = dispatcher
        self._run_at = run_at
        self._clock = clock
        self._collector = collector or MetricsCollector()
        self._alert_counts: Counter[str] = Counter()

    def record_alert(self, alert_type: str) -> None:
        """Count a system alert for the next summary (in memory, resets after each summary)."""
        self._alert_counts[alert_type] += 1

    async def tick(self) -> None:
        """Run once per trading day after `run_at`; the last-run date lives in Redis."""
        now = self._clock()
        today = now.date()
        if not market_calendar.is_trading_day(today) or now.time() < self._run_at:
            return
        try:
            if await self._cache.get(LAST_RUN_KEY) == today.isoformat():
                return
        except Exception as exc:
            system_logger.warning(
                "DailySummaryJob: could not read last run, skipping", extra={"error": str(exc)}
            )
            return
        await self.run(today)
        try:
            await self._cache.set(LAST_RUN_KEY, today.isoformat())
        except Exception as exc:
            system_logger.warning(
                "DailySummaryJob: could not store last run", extra={"error": str(exc)}
            )

    async def run(self, today: date) -> str:
        """Write the snapshot, build and send the summary; returns the body."""
        midnight = datetime.combine(today, time.min, tzinfo=market_calendar.ET)
        async with self._session_factory() as session:
            await self._collector.record_snapshot(session, self._broker)
            repo = TradeRepo(session)
            closed = await repo.closed_since(midnight)
            opened = await repo.opened_since(midnight)
            open_trades = await repo.get_open_trades()
        realized = sum((t.pnl or Decimal("0") for t in closed), Decimal("0"))

        lines = [
            f"Daily summary for {today.isoformat()}",
            "",
            f"Realized P&L today: ${realized:.2f}",
            f"Trades opened today: {len(opened)}",
            f"Trades closed today: {len(closed)}",
            "",
            f"Open positions: {len(open_trades)}",
        ]
        total_unrealized = Decimal("0")
        for trade in open_trades:
            price = await self._price(trade.symbol)
            entry = trade.entry_fill_price or trade.entry_price
            qty = trade.filled_quantity or trade.quantity
            if price is None:
                lines.append(f"  {trade.symbol}: {qty} @ ${entry}, no current price")
                continue
            unrealized = (price - entry) * qty
            total_unrealized += unrealized
            lines.append(f"  {trade.symbol}: {qty} @ ${entry}, last ${price}, P&L ${unrealized:.2f}")
        if open_trades:
            lines.append(f"Total unrealized P&L: ${total_unrealized:.2f}")
        halts = sum(self._alert_counts[a] for a in _HALT_ALERTS)
        lines += ["", f"Halts and strategy kills: {halts}", "Alerts by type:"]
        if self._alert_counts:
            lines += [f"  {k}: {v}" for k, v in sorted(self._alert_counts.items())]
        else:
            lines.append("  none")
        body = "\n".join(lines) + "\n"
        subject = f"[TradingBot] Daily summary {today.isoformat()} | P&L ${realized:.2f}"
        await self._dispatcher.notify_daily_summary(subject, body)
        self._alert_counts.clear()
        system_logger.info("DailySummaryJob: summary sent", extra={"date": today.isoformat()})
        return body

    async def _price(self, symbol: str) -> Decimal | None:
        try:
            raw = await self._cache.get(f"price:{symbol.upper()}")
            return Decimal(str(json.loads(raw)["price"])) if raw else None
        except Exception:
            return None
