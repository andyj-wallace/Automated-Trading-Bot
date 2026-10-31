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
import json
import uuid
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

from app.brokers.base import BaseBroker, PriceBar
from app.core import market_calendar
from app.core.events import publish_system_alert
from app.core.risk.growth import GrowthPolicy, effective_equity
from app.core.risk.manager import RiskManager, RiskRejectionError, TradeRequest
from app.core.strategy_engine.base import MarketData, RiskParams, Signal
from app.core.strategy_engine.indicators import avg_dollar_volume
from app.core.strategy_engine.registry import StrategyRegistry
from app.data.cache import RedisCache
from app.monitoring.logger import system_logger, trading_logger

_DEFAULT_INTERVAL = 60  # seconds between cycles
_HISTORY_LOOKBACK_DAYS = 365  # 1 year of daily bars

# After this many consecutive cycles with no price for a symbol, escalate
# from WARNING to ERROR so a stuck/misconfigured feed doesn't go unnoticed
# under routine per-cycle warnings.
_CONSECUTIVE_MISS_ESCALATION_THRESHOLD = 5
_FAILING_ALERT_THRESHOLD = 3


class StrategyScheduler:
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
            type_name = db_strategy.type
            if not self._registry.is_registered(type_name):
                system_logger.warning(
                    "StrategyScheduler: strategy type not registered — skipping",
                    extra={"strategy_id": str(db_strategy.id), "type": type_name},
                )
                continue

            symbols: list[str] = db_strategy.config.get("symbols", [])
            if not symbols:
                continue

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
                continue

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

        if signal.action == "HOLD":
            return
        if open_position:
            trading_logger.info(
                "StrategyScheduler: signal ignored, symbol already has an active trade",
                extra={"symbol": symbol, "action": signal.action, "strategy_id": str(strategy_id)},
            )
            return

        if self._max_spread_bps > 0:
            spread = await self._spread_bps(symbol)
            if spread is not None and spread > self._max_spread_bps:
                self._warn_once("wide_spread", symbol, market_calendar.trading_date(self._clock()),
                                f"StrategyScheduler: spread {spread:.1f} bps above limit, waiting")
                return

        bar_date = bars[-1].timestamp.date()
        signal_id = await self._claim_signal(strategy_id, symbol, bar_date, signal)
        if signal_id is None:
            return

        if self._min_avg_dollar_volume > 0:
            adv = avg_dollar_volume(bars, 20)
            if adv < self._min_avg_dollar_volume:
                await self._set_outcome(
                    signal_id, "INELIGIBLE",
                    detail=f"LOW_LIQUIDITY: 20-day average dollar volume {adv:.0f} < {self._min_avg_dollar_volume}",
                )
                return

        trading_logger.info(
            "StrategyScheduler: signal generated",
            extra={
                "symbol": symbol,
                "action": signal.action,
                "entry_price": str(signal.entry_price),
                "stop_loss_price": str(signal.stop_loss_price),
                "strategy_id": str(strategy_id),
            },
        )

        quantity = signal.quantity
        if (quantity is None or quantity <= 0) and signal.entry_price and signal.stop_loss_price:
            risk_params = RiskParams(
                account_balance=account_balance,
                entry_price=signal.entry_price,
                stop_loss_price=signal.stop_loss_price,
            )
            quantity = await strategy.calculate_position_size(risk_params)

        if not quantity or quantity <= 0:
            trading_logger.info(
                "StrategyScheduler: position size is zero — skipping order",
                extra={"symbol": symbol, "strategy_id": str(strategy_id)},
            )
            await self._set_outcome(signal_id, "ZERO_QUANTITY")
            return

        request = TradeRequest(
            trade_id=uuid.uuid4(),
            symbol=symbol,
            direction=signal.action,  # type: ignore[arg-type]
            quantity=Decimal(str(quantity)),
            entry_price=signal.entry_price or current_price,
            stop_loss_price=signal.stop_loss_price,
            account_balance=account_balance,
            strategy_id=strategy_id,
            take_profit_price=signal.take_profit_price,
            submit_stop_to_broker=signal.submit_stop_to_broker,
            exit_deadline=signal.exit_deadline,
        )

        try:
            await self._order_manager.submit_order(request)
        except RiskRejectionError as exc:
            await self._set_outcome(signal_id, "RISK_REJECTED", detail=exc.reason)
        except Exception as exc:
            trading_logger.warning(
                "StrategyScheduler: order submission failed",
                extra={
                    "symbol": symbol,
                    "strategy_id": str(strategy_id),
                    "error": str(exc),
                },
            )
            await self._set_outcome(signal_id, "ORDER_FAILED", detail=str(exc))
        else:
            await self._set_outcome(signal_id, "SUBMITTED", trade_id=request.trade_id)

    async def _spread_bps(self, symbol: str) -> Decimal | None:
        """Bid/ask spread in basis points from the price key; None when no usable quote is cached."""
        try:
            raw = await self._cache.get(f"price:{symbol}")
            payload = json.loads(raw) if raw else {}
            bid, ask = Decimal(str(payload["bid"])), Decimal(str(payload["ask"]))
        except (KeyError, TypeError, ValueError, ArithmeticError):
            return None
        if bid <= 0 or ask < bid:
            return None
        return (ask - bid) / ((ask + bid) / 2) * Decimal("10000")

    # ------------------------------------------------------------------
    # Internal: persistence helpers
    # ------------------------------------------------------------------

    def _signal_repo(self, session):
        if self._signal_repo_factory is not None:
            return self._signal_repo_factory(session)
        from app.db.repositories.signal_repo import SignalRepo

        return SignalRepo(session)

    def _trade_repo(self, session):
        if self._trade_repo_factory is not None:
            return self._trade_repo_factory(session)
        from app.db.repositories.trade_repo import TradeRepo

        return TradeRepo(session)

    async def _has_active_trade(self, symbol: str) -> bool:
        async with self._session_factory() as session:
            return await self._trade_repo(session).has_active_trade(symbol)

    async def _claim_signal(
        self, strategy_id: uuid.UUID, symbol: str, bar_date: date, signal: Signal
    ) -> uuid.UUID | None:
        async with self._session_factory() as session:
            signal_id = await self._signal_repo(session).claim(
                strategy_id=strategy_id,
                symbol=symbol,
                bar_date=bar_date,
                action=signal.action,
                entry_price=signal.entry_price,
                stop_loss_price=signal.stop_loss_price,
                take_profit_price=signal.take_profit_price,
            )
            await session.commit()
            return signal_id

    async def _set_outcome(
        self,
        signal_id: uuid.UUID,
        outcome: str,
        detail: str | None = None,
        trade_id: uuid.UUID | None = None,
    ) -> None:
        try:
            async with self._session_factory() as session:
                await self._signal_repo(session).set_outcome(
                    signal_id, outcome, detail=detail, trade_id=trade_id
                )
                await session.commit()
        except Exception as exc:
            system_logger.error(
                "StrategyScheduler: failed to record signal outcome",
                extra={"signal_id": str(signal_id), "outcome": outcome, "error": str(exc)},
            )

    def _warn_once(self, kind: str, symbol: str, day: date, message: str) -> None:
        key = (kind, symbol, day)
        if key not in self._warned:
            self._warned.add(key)
            system_logger.warning(message, extra={"symbol": symbol, "kind": kind})

    # ------------------------------------------------------------------
    # Internal: data helpers
    # ------------------------------------------------------------------

    async def _load_enabled_strategies(self):
        """Return enabled TradingStrategy rows from the DB."""
        try:
            from app.db.repositories.strategy_repo import StrategyRepo

            async with self._session_factory() as session:
                return await StrategyRepo(session).get_enabled()
        except Exception as exc:
            system_logger.error(
                "StrategyScheduler: failed to load strategies from DB",
                extra={"error": str(exc)},
            )
            return []

    async def _get_current_price(self, symbol: str) -> Decimal | None:
        """Price from Redis price:{symbol}, only if its timestamp is fresh."""
        try:
            raw = await self._cache.get(f"price:{symbol}")
            if not raw:
                return None
            payload = json.loads(raw)
            if not isinstance(payload, dict) or "timestamp" not in payload:
                return None
            stamp = datetime.fromisoformat(payload["timestamp"])
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=UTC)
            age = (datetime.now(UTC) - stamp).total_seconds()
            if age > self._price_stale_seconds:
                return None
            return Decimal(str(payload["price"]))
        except Exception as exc:
            system_logger.warning(
                "StrategyScheduler: Redis price lookup failed",
                extra={"symbol": symbol, "error": str(exc)},
            )
            return None

    async def _fetch_db_bars(self, symbol: str, trading_day: date) -> list[PriceBar]:
        """Completed daily sessions (strictly before trading_day) from the DB."""
        from app.db.repositories.ohlcv_repo import OHLCVRepo

        end = datetime.now(UTC)
        start = end - timedelta(days=_HISTORY_LOOKBACK_DAYS)
        async with self._session_factory() as session:
            rows = await OHLCVRepo(session).get_bars(symbol, start=start, end=end)
        return [
            PriceBar(
                timestamp=row.time,
                open=row.open,
                high=row.high,
                low=row.low,
                close=row.close,
                volume=row.volume,
                bar_size=row.bar_size,
            )
            for row in rows
            if row.time.date() < trading_day
        ]

    async def _get_intraday_bars(self, symbol: str) -> list[PriceBar]:
        """Today's completed 5-minute bars, fetched at most once per symbol per 5-minute bucket (IB pacing)."""
        now = self._clock().astimezone(UTC)
        bucket = now.replace(minute=now.minute - now.minute % 5, second=0, microsecond=0)
        key = (symbol, bucket)
        if key not in self._intraday_cache:
            try:
                fetched = await self._broker.get_intraday_bars(symbol)
            except Exception as exc:
                system_logger.warning(
                    "StrategyScheduler: intraday bar fetch failed",
                    extra={"symbol": symbol, "error": str(exc)},
                )
                return []
            today = market_calendar.trading_date(now)
            for old in [k for k in self._intraday_cache if k[1] != bucket]:
                del self._intraday_cache[old]
            self._intraday_cache[key] = [
                b for b in fetched if market_calendar.trading_date(b.timestamp) == today
            ]
        return self._intraday_cache[key]

    async def _get_historical_bars(self, symbol: str) -> list[PriceBar]:
        """Completed-session bars from the DB; [] if missing or stale."""
        trading_day = market_calendar.trading_date(self._clock())
        key = (symbol, trading_day)
        cached = self._bar_cache.get(key)
        if cached is not None:
            return cached
        try:
            bars = await self._fetch_db_bars(symbol, trading_day)
        except Exception as exc:
            system_logger.warning(
                "StrategyScheduler: DB bar fetch failed",
                extra={"symbol": symbol, "error": str(exc)},
            )
            return []
        if not bars:
            self._warn_once("no_bars", symbol, trading_day,
                            "StrategyScheduler: no completed bars for symbol — skipping")
            return []
        if bars[-1].timestamp.date() < market_calendar.previous_trading_day(trading_day):
            self._warn_once("stale_bars", symbol, trading_day,
                            "StrategyScheduler: bars are stale — skipping")
            return []
        for old in [k for k in self._bar_cache if k[1] != trading_day]:
            del self._bar_cache[old]
        self._bar_cache[key] = bars
        return bars
