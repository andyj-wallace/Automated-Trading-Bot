"""Signal gating, claiming and order submission for StrategyScheduler."""


from __future__ import annotations

import uuid
from decimal import Decimal

from app.brokers.base import PriceBar
from app.core import market_calendar
from app.core.risk.manager import RiskRejectionError, TradeRequest
from app.core.strategy_engine.base import RiskParams, Signal
from app.core.strategy_engine.indicators import avg_dollar_volume
from app.monitoring.logger import trading_logger


class SchedulerSubmitMixin:
    """Gate, claim, size and submit helpers for StrategyScheduler."""

    async def _gate_and_claim(
        self,
        strategy_id: uuid.UUID,
        symbol: str,
        bars: list[PriceBar],
        signal: Signal,
        open_position: bool,
    ) -> uuid.UUID | None:
        if signal.action == "HOLD":
            return None
        if open_position:
            trading_logger.info(
                "StrategyScheduler: signal ignored, symbol already has an active trade",
                extra={"symbol": symbol, "action": signal.action, "strategy_id": str(strategy_id)},
            )
            return None

        if self._max_spread_bps > 0:
            spread = await self._spread_bps(symbol)
            if spread is not None and spread > self._max_spread_bps:
                self._warn_once("wide_spread", symbol, market_calendar.trading_date(self._clock()),
                                f"StrategyScheduler: spread {spread:.1f} bps above limit, waiting")
                return None

        bar_date = bars[-1].timestamp.date()
        signal_id = await self._claim_signal(strategy_id, symbol, bar_date, signal)
        if signal_id is None:
            return None

        if self._min_avg_dollar_volume > 0:
            adv = avg_dollar_volume(bars, 20)
            if adv < self._min_avg_dollar_volume:
                await self._set_outcome(
                    signal_id, "INELIGIBLE",
                    detail=f"LOW_LIQUIDITY: 20-day average dollar volume {adv:.0f} < {self._min_avg_dollar_volume}",
                )
                return None
        return signal_id

    async def _size_and_submit(
        self,
        strategy,
        strategy_id: uuid.UUID,
        symbol: str,
        signal: Signal,
        signal_id: uuid.UUID,
        account_balance: Decimal,
        current_price: Decimal,
    ) -> None:
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
