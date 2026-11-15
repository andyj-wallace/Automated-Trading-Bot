"""Reconciler fill sync: late entry fills, partial entries adopted, post-close commissions."""

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import text

from app.core.execution.order_manager import CloseOutcome
from app.db.models.trade import TradeDirection, TradeStatus
from app.db.repositories.trade_repo import TradeRepo
from tests.integration import test_reconciler as base

# Fixtures and helpers shared with test_reconciler.py, bound here so pytest registers the fixtures.
BAL = base.BAL
_get = base._get
_manager = base._manager
_reconciler = base._reconciler
_request = base._request
broker = base.broker
cache = base.cache
clock = base.clock
db_engine = base.db_engine
session_factory = base.session_factory


async def _trade(session_factory, status: TradeStatus, filled: Decimal | None = None) -> uuid.UUID:
    """Insert a 10-share BUY at 10.00 in the given status, optionally with a recorded entry fill."""
    tid = uuid.uuid4()
    async with session_factory() as s:
        repo = TradeRepo(s)
        await repo.create(
            trade_id=tid, symbol="AAPL", direction=TradeDirection.BUY, quantity=Decimal("10"),
            entry_price=Decimal("10.00"), stop_loss_price=Decimal("9.50"),
            take_profit_price=Decimal("10.50"), reward_to_risk_ratio=Decimal("2"),
            risk_amount=Decimal("5"), account_balance_at_entry=BAL,
        )
        await repo.update_status(tid, status)
        if filled is not None:
            await repo.record_entry_fill(tid, "1", Decimal("10.00"), filled)
        await s.commit()
    return tid


async def test_late_entry_fill_is_synced_alerted_once_and_keeps_immutables(
    broker, session_factory, cache, clock
):
    manager = _manager(broker, session_factory, cache, protective_stops=False)
    rec = _reconciler(broker, session_factory, manager, cache, clock)
    tid = await _trade(session_factory, TradeStatus.OPEN, filled=Decimal("4"))
    broker.set_order_state(f"{tid}:entry", "WORKING", Decimal("4"), Decimal("10.00"), broker_order_id="5")
    await manager.sync_fills(tid)  # the first 4 shares are journaled
    broker.set_order_state(f"{tid}:entry", "FILLED", Decimal("10"), Decimal("10.20"))
    broker.set_position("AAPL", Decimal("10"))
    clock.state["offset"] = timedelta(minutes=2)
    before = await _get(session_factory, tid)

    await rec.run_once()
    await rec.run_once()

    after = await _get(session_factory, tid)
    assert after.status == TradeStatus.OPEN
    assert after.filled_quantity == Decimal("10")
    assert after.entry_fill_price == Decimal("10.12")
    assert len(cache.alerts("LATE_ENTRY_FILL")) == 1
    alert = cache.alerts("LATE_ENTRY_FILL")[0]
    assert alert["trade_id"] == str(tid) and alert["symbol"] == "AAPL"
    assert Decimal(alert["previous_quantity"]) == Decimal("4")
    assert Decimal(alert["filled_quantity"]) == Decimal("10")
    assert after.entry_price == before.entry_price == Decimal("10.00")
    assert after.stop_loss_price == before.stop_loss_price == Decimal("9.50")
    assert after.risk_amount == before.risk_amount == Decimal("5")


async def test_submitted_entry_working_with_fills_is_cancelled_and_adopted(
    broker, session_factory, cache, clock
):
    manager = _manager(broker, session_factory, cache, protective_stops=False)
    rec = _reconciler(broker, session_factory, manager, cache, clock)
    tid = await _trade(session_factory, TradeStatus.SUBMITTED)
    broker.set_order_state(
        f"{tid}:entry", "WORKING", Decimal("4"), Decimal("10.00"), broker_order_id="77"
    )
    clock.state["offset"] = timedelta(minutes=2)

    report = await rec.run_once()

    trade = await _get(session_factory, tid)
    assert report.counts["entry_adopted"] == 1
    assert (await broker.find_order_by_ref(f"{tid}:entry")).status == "PARTIAL"
    assert trade.status == TradeStatus.OPEN
    assert trade.filled_quantity == Decimal("4") and trade.entry_fill_price == Decimal("10.00")


async def test_post_close_commission_updates_fill_row_and_not_pnl(
    broker, session_factory, cache, clock
):
    manager = _manager(broker, session_factory, cache, protective_stops=False)
    req = _request()
    await manager.submit_order(req)
    assert await manager.close_position(req.trade_id, "MANUAL") == CloseOutcome.CLOSED
    closed = await _get(session_factory, req.trade_id)
    assert closed.status == TradeStatus.CLOSED
    exit_exec = next(e for e in broker.executions if e.order_ref == f"{req.trade_id}:exit")
    broker.set_execution_commission(exit_exec.exec_id, Decimal("1.00"))
    clock.state["offset"] = timedelta(minutes=2)

    async def exit_commission():
        async with session_factory() as s:
            row = await s.execute(
                text("SELECT commission FROM trade_fills WHERE trade_id = :t AND leg = 'EXIT'"),
                {"t": req.trade_id},
            )
            return row.scalar_one()

    assert await exit_commission() is None
    await _reconciler(broker, session_factory, manager, cache, clock).run_once()

    assert await exit_commission() == Decimal("1.00")
    assert (await _get(session_factory, req.trade_id)).pnl == closed.pnl
