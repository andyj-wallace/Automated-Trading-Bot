"""
StrategyScheduler — runs enabled strategies on a fixed interval.

Each cycle:
  1. Fetch account balance from the broker (needed for risk sizing).
  2. Fetch aggregate risk from RiskMonitor (needed for portfolio gate).
  3. Load enabled strategies from the DB (fresh each cycle → enable/disable
     takes effect without restart).
  4. For each enabled strategy:
       a. Read config.symbols list.
       b. For each symbol:
            - Get current price (Redis price:{symbol}, fallback: latest DB bar close).
            - Get historical bars (TimescaleDB via OHLCVRepo, fallback: broker).
            - Call strategy.generate_signal(market_data).
            - If BUY/SELL: size the position and call order_manager.submit_order().

Runs as a background asyncio task, started on app startup (alongside PositionMonitor).

Errors in individual (strategy, symbol) pairs are logged and skipped so one
bad strategy cannot abort the entire cycle.

Depends on:
  StrategyRegistry (11.1), StrategyRepo (3.8), OHLCVRepo (5.3),
  RedisCache (5.1), RiskMonitor (6.4), OrderManager (7.1)
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.brokers.base import BaseBroker, PriceBar
from app.core.risk.manager import RiskManager, TradeRequest
from app.core.strategy_engine.base import MarketData, RiskParams, Signal
from app.core.strategy_engine.registry import StrategyRegistry
from app.data.cache import RedisCache
from app.monitoring.logger import system_logger, trading_logger

_DEFAULT_INTERVAL = 60  # seconds between cycles
_HISTORY_LOOKBACK_DAYS = 365  # 1 year of daily bars


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
    ) -> None:
        self._registry = registry
        self._broker = broker
        self._risk_manager = risk_manager
        self._cache = cache
        self._session_factory = session_factory
        self._order_manager = order_manager
        self._interval = interval_seconds
        self._task: asyncio.Task | None = None
        self._running = False

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
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        system_logger.info("StrategyScheduler stopped")

    # ------------------------------------------------------------------
    # Internal: main loop
    # ------------------------------------------------------------------

    async def _run(self) -> None:
        while self._running:
            try:
                await self._run_cycle()
            except Exception as exc:
                system_logger.error(
                    "StrategyScheduler: unhandled cycle error",
                    extra={"error": str(exc)},
                )
            try:
                await asyncio.sleep(self._interval)
            except asyncio.CancelledError:
                break

    async def _run_cycle(self) -> None:
        """Execute one full pass through all enabled strategies."""
        # 1. Account balance (needed for risk sizing and portfolio gate)
        account_balance = Decimal("0")
        if self._broker.is_connected():
            try:
                summary = await self._broker.get_account_summary()
                account_balance = summary.net_liquidation
            except Exception as exc:
                system_logger.warning(
                    "StrategyScheduler: could not fetch account balance",
                    extra={"error": str(exc)},
                )

        # 2. Aggregate risk (needed for portfolio gate)
        current_aggregate_risk = await self._get_aggregate_risk(account_balance)

        # 3. Load enabled strategies from DB
        enabled_strategies = await self._load_enabled_strategies()
        if not enabled_strategies:
            return

        system_logger.info(
            "StrategyScheduler: cycle started",
            extra={
                "strategy_count": len(enabled_strategies),
                "account_balance": str(account_balance),
                "aggregate_risk": str(current_aggregate_risk),
            },
        )

        # 4. Run each (strategy, symbol) pair
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
                        current_aggregate_risk=current_aggregate_risk,
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
        current_aggregate_risk: Decimal,
    ) -> None:
        """Run one strategy against one symbol and submit if a signal fires."""
        # Get current price
        current_price = await self._get_current_price(symbol)
        if current_price is None:
            system_logger.warning(
                "StrategyScheduler: no price data for symbol — skipping",
                extra={"symbol": symbol},
            )
            return

        # Get historical bars
        bars = await self._get_historical_bars(symbol)
        if not bars:
            system_logger.warning(
                "StrategyScheduler: no historical bars for symbol — skipping",
                extra={"symbol": symbol},
            )
            return

        market_data = MarketData(
            symbol=symbol,
            current_price=current_price,
            bars=bars,
            timestamp=datetime.now(timezone.utc),
        )

        signal: Signal = await strategy.generate_signal(market_data)

        if signal.action == "HOLD":
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

        # Size position if strategy did not set quantity
        quantity = signal.quantity
        if quantity is None or quantity <= 0:
            if signal.entry_price and signal.stop_loss_price:
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
            return

        # Build and submit trade request
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
        )

        try:
            await self._order_manager.submit_order(
                request,
                current_aggregate_risk=current_aggregate_risk,
            )
        except Exception as exc:
            trading_logger.warning(
                "StrategyScheduler: order submission failed",
                extra={
                    "symbol": symbol,
                    "strategy_id": str(strategy_id),
                    "error": str(exc),
                },
            )

    # ------------------------------------------------------------------
    # Internal: helpers
    # ------------------------------------------------------------------

    async def _get_aggregate_risk(self, account_balance: Decimal) -> Decimal:
        """Query current aggregate risk via RiskMonitor."""
        if account_balance == Decimal("0"):
            return Decimal("0")
        try:
            from app.core.risk.monitor import RiskMonitor

            async with self._session_factory() as session:
                monitor = RiskMonitor()
                status = await monitor.check_exposure(session, account_balance)
                return status.aggregate_risk_amount
        except Exception as exc:
            system_logger.warning(
                "StrategyScheduler: could not fetch aggregate risk",
                extra={"error": str(exc)},
            )
            return Decimal("0")

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
        """
        Try Redis price:{symbol} first.
        Falls back to broker historical data's most recent close.
        """
        try:
            raw = await self._cache.get(f"price:{symbol}")
            if raw:
                payload = json.loads(raw)
                price_val = payload.get("price", payload) if isinstance(payload, dict) else payload
                return Decimal(str(price_val))
        except Exception:
            pass
        return None

    async def _get_historical_bars(self, symbol: str) -> list[PriceBar]:
        """
        Fetch historical bars from TimescaleDB.
        Falls back to broker.get_historical_data() if no DB rows.
        """
        try:
            from datetime import timezone as tz

            from app.db.models.ohlcv_bar import OHLCVBar
            from app.db.repositories.ohlcv_repo import OHLCVRepo

            end = datetime.now(tz.utc)
            start = end - timedelta(days=_HISTORY_LOOKBACK_DAYS)

            async with self._session_factory() as session:
                repo = OHLCVRepo(session)
                db_bars = await repo.get_bars(symbol, start=start, end=end)

            if db_bars:
                return [
                    PriceBar(
                        timestamp=bar.time,
                        open=bar.open,
                        high=bar.high,
                        low=bar.low,
                        close=bar.close,
                        volume=bar.volume,
                        bar_size=bar.bar_size,
                    )
                    for bar in db_bars
                ]
        except Exception as exc:
            system_logger.warning(
                "StrategyScheduler: DB bar fetch failed, trying broker",
                extra={"symbol": symbol, "error": str(exc)},
            )

        # Fallback: fetch directly from broker
        try:
            return await self._broker.get_historical_data(symbol)
        except Exception as exc:
            system_logger.warning(
                "StrategyScheduler: broker bar fetch also failed",
                extra={"symbol": symbol, "error": str(exc)},
            )
            return []
