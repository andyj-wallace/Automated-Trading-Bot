"""TradingEngine: builds, starts and stops every long-lived trading component."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from contextlib import suppress
from datetime import datetime, time
from decimal import Decimal

from app.brokers.ibkr.client import LiveTradingGuardError
from app.config import Settings
from app.core import market_calendar
from app.core.engine_lock import EngineLock
from app.core.events import RISK_UPDATES, SYSTEM_ALERTS, TRADE_EVENTS, publish_system_alert
from app.core.jobs.bar_refresh import BarRefreshJob
from app.core.jobs.daily_summary import DailySummaryJob
from app.core.risk.costs import CostModel
from app.core.risk.growth import GrowthPolicy, effective_equity
from app.core.tasks import PeriodicTask, spawn, supervised_subscribe
from app.core.trading_state import TradingState
from app.monitoring.logger import error_logger, system_logger


class TradingEngine:
    """Owns the broker, cache, risk/execution components and background tasks."""

    def __init__(
        self,
        settings: Settings,
        *,
        clock: Callable[[], datetime] = market_calendar.now_et,
        is_market_open: Callable[[], bool] = market_calendar.is_market_open,
        feed_sync_interval: float = 60,
        bar_refresh_interval: float = 60,
        monitor_resync_interval: float = 5.0,
        lock_ttl_seconds: int = 30,
        lock_renew_interval: float = 10.0,
        lock_retry_interval: float = 5.0,
    ) -> None:
        from app.core.execution.order_manager import OrderManager
        from app.core.execution.position_monitor import PositionMonitor
        from app.core.execution.reconciler import Reconciler
        from app.core.risk.manager import RiskManager
        from app.core.risk.monitor import RiskMonitor, RiskMonitorConfig
        from app.core.strategy_engine.builtin import load_builtin_strategies
        from app.core.strategy_engine.registry import registry
        from app.core.strategy_engine.scheduler import StrategyScheduler
        from app.data.feed import MarketDataFeed
        from app.db.session import AsyncSessionFactory
        from app.dependencies import _build_broker, _build_cache, _build_circuit_breaker
        from app.notifications.dispatcher import NotificationDispatcher

        load_builtin_strategies()
        self.settings = settings
        self._clock = clock
        self._is_market_open = is_market_open
        self.started = False
        self._tasks: set[asyncio.Task] = set()

        self.broker = _build_broker()
        self.cache = _build_cache()
        self.circuit_breaker = _build_circuit_breaker()
        self.cost_model = CostModel.from_settings(settings)
        self.growth = GrowthPolicy.from_settings(settings)
        self.risk_manager = RiskManager(
            min_reward_to_risk=settings.min_reward_to_risk,
            max_portfolio_risk_pct=settings.max_portfolio_risk_pct,
            circuit_breaker=self.circuit_breaker,
            cost_model=self.cost_model,
            max_breakeven_win_rate=settings.max_breakeven_win_rate,
            growth=self.growth,
        )
        self.order_manager = OrderManager(
            broker=self.broker,
            risk_manager=self.risk_manager,
            session_factory=AsyncSessionFactory,
            cache=self.cache,
            circuit_breaker=self.circuit_breaker,
            is_market_open=is_market_open,
            protective_stops=settings.broker_protective_stops,
            growth=self.growth,
            account_type=settings.account_type,
            cost_model=self.cost_model,
        )
        self.feed = MarketDataFeed(self.broker, self.cache, AsyncSessionFactory)
        self.position_monitor = PositionMonitor(
            cache=self.cache,
            session_factory=AsyncSessionFactory,
            order_manager=self.order_manager,
            stale_trade_max_days=settings.stale_trade_max_days,
            last_tick_at=self.feed.last_tick_at,
            price_stale_seconds=settings.price_stale_seconds,
            is_market_open=is_market_open,
            clock=clock,
            resync_interval_seconds=monitor_resync_interval,
        )
        self.feed.add_listener(self.position_monitor.on_price_update)
        self.reconciler = Reconciler(
            self.broker,
            AsyncSessionFactory,
            self.order_manager,
            self.cache,
            clock=clock,
            is_market_open=is_market_open,
        )
        self._reconciler_task = PeriodicTask(
            "reconciler", settings.reconcile_interval_seconds, self.reconciler.run_once
        )
        if hasattr(self.broker, "add_reconnect_listener"):
            self.broker.add_reconnect_listener(self._on_broker_reconnected)
        self._feed_sync = PeriodicTask("feed_sync", feed_sync_interval, self.feed.sync)
        self.bar_refresh = BarRefreshJob(
            self.broker,
            AsyncSessionFactory,
            self.cache,
            run_at=settings.bar_refresh_time_et,
            clock=clock,
        )
        self._bar_refresh_task = PeriodicTask("bar_refresh", bar_refresh_interval, self.bar_refresh.tick)
        self.trading_state = TradingState(self.cache)
        self.scheduler = StrategyScheduler(
            registry=registry,
            broker=self.broker,
            risk_manager=self.risk_manager,
            cache=self.cache,
            session_factory=AsyncSessionFactory,
            order_manager=self.order_manager,
            interval_seconds=settings.scheduler_interval_seconds,
            clock=clock,
            eval_start=settings.strategy_eval_start_et,
            eval_end=settings.strategy_eval_end_et,
            price_stale_seconds=settings.price_stale_seconds,
            is_paused=self.trading_state.is_paused,
            capital_limit=settings.trading_capital_limit,
            growth=self.growth,
            max_spread_bps=settings.max_spread_bps,
            min_avg_dollar_volume=settings.min_avg_dollar_volume,
        )
        self.risk_monitor = RiskMonitor(
            config=RiskMonitorConfig(max_aggregate_risk_pct=settings.max_portfolio_risk_pct),
            cache=self.cache,
        )
        self.dispatcher = NotificationDispatcher(settings)
        self.daily_summary = DailySummaryJob(
            self.broker,
            AsyncSessionFactory,
            self.cache,
            self.dispatcher,
            run_at=settings.daily_summary_time_et,
            clock=clock,
            cost_model=self.cost_model,
            growth=self.growth,
            capital_limit=settings.trading_capital_limit,
        )
        self._daily_summary_task = PeriodicTask("daily_summary", 60, self.daily_summary.tick)
        self._session_factory = AsyncSessionFactory
        self._risk_task: asyncio.Task | None = None
        self._notifier_task: asyncio.Task | None = None
        self.lock = EngineLock(self.cache, ttl_seconds=lock_ttl_seconds)
        self._lock_renew_interval = lock_renew_interval
        self._lock_retry_interval = lock_retry_interval
        self._lock_task: asyncio.Task | None = None
        self._leader = False
        self._last_renewed = 0.0

    def spawn(self, coro, name: str) -> asyncio.Task:
        """Create a tracked background task owned by the engine."""
        return spawn(coro, name=name, registry=self._tasks)

    async def get_equity(self) -> Decimal:
        """Net liquidation capped at TRADING_CAPITAL_LIMIT; 0 when the broker is unreachable."""
        if self.broker.is_connected():
            try:
                summary = await self.broker.get_account_summary()
                return effective_equity(summary, self.settings.trading_capital_limit)
            except Exception:
                pass
        return Decimal("0")

    async def _handle_notification(self, msg: dict) -> None:
        event = json.loads(msg["data"])
        if event.get("event") == "system_alert":
            self.daily_summary.record_alert(str(event.get("payload", {}).get("alert_type", "UNKNOWN")))
        await self.dispatcher.dispatch(event)

    async def _sync_feed_at_start(self) -> None:
        try:
            await self.feed.sync()
        except Exception:
            system_logger.exception("Initial feed sync failed, periodic sync will retry")

    async def rehydrate_circuit_breaker(self) -> None:
        """Rebuild the breaker's day and streak state from the trades table."""
        from app.db.repositories.trade_repo import TradeRepo

        now = self._clock()
        day = market_calendar.trading_date(now)
        midnight = datetime.combine(day, time.min, tzinfo=market_calendar.ET)
        async with self._session_factory() as session:
            repo = TradeRepo(session)
            closed_today = await repo.closed_since(midnight)
            recents = await repo.recent_pnls_by_strategy(self.settings.max_consecutive_losses)
        realized = sum((t.pnl or Decimal("0") for t in closed_today), Decimal("0"))
        balance = await self.get_equity()
        if balance > 0:
            starting = balance - realized
        else:
            starting = max(
                (t.account_balance_at_entry for t in closed_today), default=Decimal("0")
            )
        self.circuit_breaker.rehydrate(
            realized_pnl_today=realized,
            day_starting_balance=starting,
            recent_pnls_by_strategy=recents,
            now=now,
        )
        system_logger.info(
            "Circuit breaker rehydrated",
            extra={
                "realized_pnl_today": str(realized),
                "day_starting_balance": str(starting),
                "halted": self.circuit_breaker.is_halted(now=now),
            },
        )

    async def _on_broker_reconnected(self) -> None:
        if self._leader:
            await self.reconciler.run_once()

    async def fresh_price(self, symbol: str) -> Decimal | None:
        """Price from Redis price:{symbol} if no older than price_stale_seconds, else None."""
        from datetime import UTC

        try:
            raw = await self.cache.get(f"price:{symbol.upper()}")
            if not raw:
                return None
            payload = json.loads(raw)
            stamp = datetime.fromisoformat(payload["timestamp"])
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=UTC)
            if (datetime.now(UTC) - stamp).total_seconds() > self.settings.price_stale_seconds:
                return None
            return Decimal(str(payload["price"]))
        except Exception:
            return None

    def market_open(self) -> bool:
        return self._is_market_open()

    def in_eval_window(self) -> bool:
        return market_calendar.in_session_window(
            self.settings.strategy_eval_start_et, self.settings.strategy_eval_end_et, self._clock()
        )

    @property
    def is_leader(self) -> bool:
        """True while this process holds the engine lock and runs the trading loops."""
        return self._leader

    async def _try_acquire_leadership(self) -> bool:
        try:
            acquired = await self.lock.acquire()
        except Exception as exc:
            system_logger.warning("Engine lock unavailable", extra={"error": str(exc)})
            return False
        if acquired:
            self._leader = True
            self._last_renewed = asyncio.get_running_loop().time()
            await self._start_trading_parts()
        return acquired

    async def _start_trading_parts(self) -> None:
        """Everything that places or closes orders; runs only while we hold the lock."""
        try:
            await self.rehydrate_circuit_breaker()
        except Exception:
            system_logger.exception("Circuit breaker rehydrate failed, starting with a clean breaker")
        try:
            await self.reconciler.run_once()
        except Exception:
            system_logger.exception("Initial reconcile failed, periodic run will retry")
        await self.position_monitor.start()
        self._bar_refresh_task.start()
        self._reconciler_task.start()
        self._daily_summary_task.start()
        await self.scheduler.start()
        system_logger.info("Trading loops started (engine lock held)")

    async def _stop_trading_parts(self) -> None:
        await self._reconciler_task.stop()
        await self.scheduler.stop()
        await self.position_monitor.stop()
        await self._bar_refresh_task.stop()
        await self._daily_summary_task.stop()

    async def _lock_loop(self) -> None:
        """Renew the lease while leader; otherwise keep trying to acquire it."""
        loop = asyncio.get_running_loop()
        last_logged = loop.time()
        while True:
            if not self._leader:
                await asyncio.sleep(self._lock_retry_interval)
                if await self._try_acquire_leadership():
                    continue
                if loop.time() - last_logged >= 60:
                    last_logged = loop.time()
                    system_logger.info("Waiting for the engine lock held by another instance")
                continue
            await asyncio.sleep(self._lock_renew_interval)
            try:
                renewed = await self.lock.renew()
            except Exception as exc:
                renewed = False
                still_valid = loop.time() - self._last_renewed < self.lock.ttl_seconds * 2 / 3
                system_logger.warning("Engine lock renewal errored", extra={"error": str(exc)})
                if still_valid:
                    continue
            if renewed:
                self._last_renewed = loop.time()
                continue
            self._leader = False
            await self._stop_trading_parts()
            await publish_system_alert(
                self.cache,
                "ENGINE_LOCK_LOST",
                "Another instance took the engine lock: scheduler, reconciler and monitor stopped",
            )

    async def _seed_strategies(self) -> None:
        from app.core.strategy_engine.registry import registry
        from app.core.strategy_engine.seed import seed_strategies

        try:
            created = await seed_strategies(registry, self._session_factory)
            if created:
                system_logger.info("Seeded default strategies", extra={"count": created})
        except Exception:
            system_logger.exception("Strategy seeding failed")

    async def start(self) -> None:
        """Start all components in dependency order."""
        await self._seed_strategies()
        try:
            await self.broker.connect()
        except LiveTradingGuardError as exc:
            error_logger.critical("Live trading guard triggered, trading components not started")
            await publish_system_alert(self.cache, "BROKER_ACCOUNT_MODE_MISMATCH", str(exc))
            # The notifier is not subscribed yet (pub/sub keeps nothing), so email this one directly.
            try:
                await self.dispatcher.notify_system_alert("BROKER_ACCOUNT_MODE_MISMATCH", str(exc))
            except Exception:
                system_logger.exception("Could not send the account-mode alert")
            return
        except Exception as exc:
            system_logger.warning(
                "Broker connection failed at startup, health will show disconnected",
                extra={
                    "error": str(exc),
                    "exception_type": type(exc).__name__,
                    "exception_repr": repr(exc),
                },
            )
        await self._sync_feed_at_start()
        self._feed_sync.start()
        self._risk_task = self.spawn(
            self.risk_monitor.run_loop(self._session_factory, self.get_equity),
            name="risk_monitor",
        )
        self._notifier_task = self.spawn(
            supervised_subscribe(
                self.cache,
                [RISK_UPDATES, TRADE_EVENTS, SYSTEM_ALERTS],
                self._handle_notification,
                name="notification_dispatcher",
            ),
            name="notification_dispatcher",
        )
        self.started = True
        await self._try_acquire_leadership()
        self._lock_task = self.spawn(self._lock_loop(), name="engine_lock")
        system_logger.info("Trading engine started", extra={"leader": self._leader})

    async def stop(self) -> None:
        """Stop all components in reverse dependency order."""
        self.started = False
        if self._lock_task is not None and not self._lock_task.done():
            self._lock_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._lock_task
        if self._leader:
            await self._stop_trading_parts()
            self._leader = False
            try:
                await self.lock.release()
            except Exception:
                system_logger.warning("Could not release the engine lock, it will expire")
        await self._feed_sync.stop()
        await self.broker.disconnect()
        if self._notifier_task is not None and not self._notifier_task.done():
            self._notifier_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._notifier_task
        if self._risk_task is not None and not self._risk_task.done():
            self.risk_monitor.stop()
            self._risk_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._risk_task
        await self.cache.close()
        system_logger.info("Trading engine stopped")
