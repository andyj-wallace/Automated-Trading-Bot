"""
StrategyScheduler — runs enabled strategies on a fixed interval.

Each cycle (only inside the configured ET evaluation window of a trading day,
while trading is not paused and the broker is connected):
  1. Fetch the account balance (needed for risk sizing).
  2. Load enabled strategies from the DB (fresh each cycle).
  3. For each (strategy, symbol):
       - Current price: Redis key price:{symbol}, only if fresh.
       - Bars: TimescaleDB only, completed sessions only (never today's bar).
       - generate_signal(); skip if the symbol already has an active trade.
       - BUY/SELL: claim (strategy, symbol, bar date, action) in strategy_signals
         so a signal acts at most once, then size and submit via OrderManager.

The scheduler never calls the broker for market data; bar freshness is the
BarRefreshJob's responsibility and price freshness the MarketDataFeed's.

Aggregate portfolio risk and the one-active-trade-per-symbol rule are enforced
inside OrderManager.submit_order under its submission lock.
"""


from __future__ import annotations

import asyncio
import json as json
import uuid
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import UTC, date, datetime, time
from datetime import timedelta as timedelta
from decimal import Decimal

from app.brokers.base import BaseBroker, PriceBar
from app.core import market_calendar
from app.core.events import publish_system_alert
from app.core.risk.growth import GrowthPolicy, effective_equity
from app.core.risk.manager import RiskManager
from app.core.risk.manager import RiskRejectionError as RiskRejectionError
from app.core.risk.manager import TradeRequest as TradeRequest
from app.core.strategy_engine.base import MarketData, Signal
from app.core.strategy_engine.base import RiskParams as RiskParams
from app.core.strategy_engine.indicators import avg_dollar_volume as avg_dollar_volume
from app.core.strategy_engine.registry import StrategyRegistry
from app.core.strategy_engine.scheduler_data import _HISTORY_LOOKBACK_DAYS as _HISTORY_LOOKBACK_DAYS
from app.core.strategy_engine.scheduler_data import SchedulerDataMixin
from app.core.strategy_engine.scheduler_persistence import SchedulerPersistenceMixin
from app.core.strategy_engine.scheduler_submit import SchedulerSubmitMixin
from app.data.cache import RedisCache
from app.monitoring.logger import system_logger
from app.monitoring.logger import trading_logger as trading_logger

_DEFAULT_INTERVAL = 60  # seconds between cycles

# After this many consecutive cycles with no price for a symbol, escalate
# from WARNING to ERROR so a stuck/misconfigured feed doesn't go unnoticed
# under routine per-cycle warnings.
_CONSECUTIVE_MISS_ESCALATION_THRESHOLD = 5
_FAILING_ALERT_THRESHOLD = 3

class StrategyScheduler(SchedulerSubmitMixin, SchedulerPersistenceMixin, SchedulerDataMixin):
    """
    Periodic scheduler that drives all enabled strategies.

    Usage:
        scheduler = StrategyScheduler(
            registry, broker, risk_manager, cache, session_factory, order_manager
        )
        await scheduler.start()
        ...
        await scheduler.stop()
    """

    def __init__(
        self,
        registry: StrategyRegistry,
        broker: BaseBroker,
        risk_manager: RiskManager,
        cache: RedisCache,
        session_factory,
        order_manager,
        interval_seconds: int = _DEFAULT_INTERVAL,
        *,
        clock: Callable[[], datetime] = market_calendar.now_et,
        eval_start: time = time(9, 45),
        eval_end: time = time(15, 45),
        price_stale_seconds: int = 120,
        is_paused: Callable[[], Awaitable[bool]] | None = None,
        signal_repo_factory=None,
        trade_repo_factory=None,
        capital_limit: Decimal | None = None,
        growth: GrowthPolicy | None = None,
        max_spread_bps: Decimal = Decimal("0"),
        min_avg_dollar_volume: Decimal = Decimal("0"),
    ) -> None:
        self._capital_limit = capital_limit
        self._growth = growth
        self._growth_active: bool | None = None
        self._max_spread_bps = max_spread_bps
        self._min_avg_dollar_volume = min_avg_dollar_volume
        self._registry = registry
        self._broker = broker
        self._risk_manager = risk_manager
        self._cache = cache
        self._session_factory = session_factory
        self._order_manager = order_manager
        self._interval = interval_seconds
        self._task: asyncio.Task | None = None
        self._running = False
        self._consecutive_price_misses: dict[str, int] = {}
        self._clock = clock
        self._eval_start = eval_start
        self._eval_end = eval_end
        self._price_stale_seconds = price_stale_seconds
        self._is_paused = is_paused
        self._signal_repo_factory = signal_repo_factory
        self._trade_repo_factory = trade_repo_factory
        self._bar_cache: dict[tuple[str, date], list[PriceBar]] = {}
        self._bar_generation: dict[str, int] = {}
        self._intraday_cache: dict[tuple[str, datetime], list[PriceBar]] = {}
        self._warned: set[tuple[str, str, date]] = set()
        self.last_cycle_at: datetime | None = None
        self._consecutive_failures = 0

    async def start(self) -> None:
        """Start the background scheduler loop."""
        self._running = True
        self._task = asyncio.create_task(self._run(), name="strategy_scheduler")
        system_logger.info(
            "StrategyScheduler started",
            extra={"interval_seconds": self._interval},
        )

    async def stop(self) -> None:
        """Stop the scheduler and wait for the current cycle to finish."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
        system_logger.info("StrategyScheduler stopped")

    # ------------------------------------------------------------------
    # Internal: main loop
    # ------------------------------------------------------------------

    async def _run(self) -> None:
        while self._running:
            try:
                await self._run_cycle()
                self._consecutive_failures = 0
            except Exception as exc:
                system_logger.error(
                    "StrategyScheduler: unhandled cycle error",
                    extra={"error": str(exc)},
                )
                self._consecutive_failures += 1
                if self._consecutive_failures == _FAILING_ALERT_THRESHOLD:
                    await publish_system_alert(
                        self._cache,
                        "SCHEDULER_FAILING",
                        f"Strategy scheduler failed {_FAILING_ALERT_THRESHOLD} cycles in a row: {exc}",
                    )
            try:
                await asyncio.sleep(self._interval)
            except asyncio.CancelledError:
                break

    async def _run_cycle(self) -> None:
        """Execute one full pass through all enabled strategies."""
        now = self._clock()
        trading_day = market_calendar.trading_date(now)
        if not market_calendar.in_session_window(self._eval_start, self._eval_end, now):
            return
        if self._is_paused is not None and await self._is_paused():
            return
        if not self._broker.is_connected():
            self._warn_once("broker_disconnected", "", trading_day,
                            "StrategyScheduler: broker disconnected, cycles skipped")
            return

        try:
            summary = await self._broker.get_account_summary()
            account_balance = effective_equity(summary, self._capital_limit)
        except Exception as exc:
            system_logger.warning(
                "StrategyScheduler: could not fetch account balance",
                extra={"error": str(exc)},
            )
            return
        if account_balance <= 0:
            system_logger.warning("StrategyScheduler: account balance is not positive")
            return

        if self._growth is not None:
            active = self._growth.is_active(account_balance)
            if self._growth_active is not None and active != self._growth_active:
                await publish_system_alert(
                    self._cache,
                    "GROWTH_MODE_CHANGED",
                    f"Growth mode {'ON' if active else 'OFF'}: equity ${account_balance:.2f}, "
                    f"exit level ${self._growth.exit_equity}",
                )
            self._growth_active = active

        self.last_cycle_at = datetime.now(UTC)
        enabled_strategies = await self._load_enabled_strategies()
        if not enabled_strategies:
            return

        system_logger.info(
            "StrategyScheduler: cycle started",
            extra={
                "strategy_count": len(enabled_strategies),
                "account_balance": str(account_balance),
            },
        )

        for db_strategy in enabled_strategies:
            await self._run_strategy(db_strategy, account_balance)

    async def _run_strategy(self, db_strategy, account_balance: Decimal) -> None:
        type_name = db_strategy.type
        if not self._registry.is_registered(type_name):
            system_logger.warning(
                "StrategyScheduler: strategy type not registered — skipping",
                extra={"strategy_id": str(db_strategy.id), "type": type_name},
            )
            return

        symbols: list[str] = db_strategy.config.get("symbols", [])
        if not symbols:
            return

        try:
            strategy = self._registry.build(type_name, db_strategy.config)
        except Exception as exc:
            system_logger.error(
                "StrategyScheduler: failed to instantiate strategy",
                extra={
                    "strategy_id": str(db_strategy.id),
                    "type": type_name,
                    "error": str(exc),
                },
            )
            return

        for symbol in symbols:
            try:
                await self._run_strategy_for_symbol(
                    strategy=strategy,
                    strategy_id=db_strategy.id,
                    symbol=symbol.upper(),
                    account_balance=account_balance,
                )
            except Exception as exc:
                system_logger.error(
                    "StrategyScheduler: error running strategy for symbol",
                    extra={
                        "strategy_id": str(db_strategy.id),
                        "symbol": symbol,
                        "error": str(exc),
                    },
                )

    async def _run_strategy_for_symbol(
        self,
        strategy,
        strategy_id: uuid.UUID,
        symbol: str,
        account_balance: Decimal,
    ) -> None:
        """Run one strategy against one symbol and submit if a signal fires."""
        current_price = await self._get_current_price(symbol)
        if current_price is None:
            miss_count = self._consecutive_price_misses.get(symbol, 0) + 1
            self._consecutive_price_misses[symbol] = miss_count
            log = (
                system_logger.error
                if miss_count >= _CONSECUTIVE_MISS_ESCALATION_THRESHOLD
                else system_logger.warning
            )
            log(
                "StrategyScheduler: no fresh price for symbol — "
                "strategy evaluation skipped for this cycle",
                extra={"symbol": symbol, "consecutive_misses": miss_count},
            )
            return
        self._consecutive_price_misses.pop(symbol, None)

        bars = await self._get_historical_bars(symbol)
        if not bars:
            return

        extra_bars: dict[str, list[PriceBar]] = {}
        for extra_symbol in strategy.required_extra_symbols():
            extra_symbol = extra_symbol.upper()
            if extra_symbol == symbol:
                continue
            fetched = await self._get_historical_bars(extra_symbol)
            if fetched:
                extra_bars[extra_symbol] = fetched
            else:
                system_logger.warning(
                    "StrategyScheduler: no usable bars for required extra "
                    "symbol — strategy will see it as missing",
                    extra={"symbol": symbol, "extra_symbol": extra_symbol},
                )

        need = strategy.min_history_bars()
        if need and (len(bars) < need or any(len(b) < need for b in extra_bars.values())):
            self._warn_once("short_history", symbol, market_calendar.trading_date(self._clock()),
                            f"StrategyScheduler: fewer completed bars than the strategy needs ({need}); "
                            "it may hold until more history loads")

        intraday_bars: list[PriceBar] = []
        if strategy.needs_intraday:
            intraday_bars = await self._get_intraday_bars(symbol)
            if not intraday_bars:
                return

        open_position = await self._has_active_trade(symbol)
        market_data = MarketData(
            symbol=symbol,
            current_price=current_price,
            bars=bars,
            timestamp=datetime.now(UTC),
            extra_bars=extra_bars,
            open_position=open_position,
            intraday_bars=intraday_bars,
        )

        signal: Signal = await strategy.generate_signal(market_data)

        signal_id = await self._gate_and_claim(strategy_id, symbol, bars, signal, open_position)
        if signal_id is None:
            return
        await self._size_and_submit(
            strategy, strategy_id, symbol, signal, signal_id, account_balance, current_price,
        )
