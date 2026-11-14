from __future__ import annotations

import uuid
from collections.abc import Collection
from datetime import date

from app.core.strategy_engine.base import Signal
from app.monitoring.logger import system_logger


class SchedulerPersistenceMixin:
    """Persistence helpers for StrategyScheduler: repo access, signal claims and outcomes."""

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

    async def _get_active_trade(self, symbol: str):
        async with self._session_factory() as session:
            return await self._trade_repo(session).get_active_trade(symbol)

    async def _claim_signal(
        self,
        strategy_id: uuid.UUID,
        symbol: str,
        bar_date: date,
        signal: Signal,
        *,
        reclaim_outcomes: Collection[str] = (),
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
                reclaim_outcomes=reclaim_outcomes,
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
