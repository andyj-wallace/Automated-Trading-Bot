"""MockBroker execution ids stay unique across broker instances and fit the trade_fills column."""

import uuid
from decimal import Decimal

from app.brokers.base import OrderRequest
from app.brokers.mock.client import MockBroker

_MAX_EXEC_ID_LENGTH = 64


async def _fill_entries(broker: MockBroker, count: int) -> list[str]:
    await broker.connect()
    for _ in range(count):
        trade_id = uuid.uuid4()
        await broker.place_order(
            OrderRequest(
                trade_id=trade_id,
                symbol="AAPL",
                direction="BUY",
                quantity=Decimal("5"),
                order_ref=f"{trade_id}:entry",
            )
        )
    return [e.exec_id for e in await broker.get_executions()]


async def test_exec_ids_differ_across_broker_instances() -> None:
    first = await _fill_entries(MockBroker(), 1)
    second = await _fill_entries(MockBroker(), 1)
    assert len(first) == 1 and len(second) == 1
    assert first[0] != second[0]


async def test_exec_ids_stay_within_trade_fills_column_length() -> None:
    ids = await _fill_entries(MockBroker(), 3) + await _fill_entries(MockBroker(), 3)
    assert len(ids) == 6
    assert len(set(ids)) == 6
    assert all(len(exec_id) <= _MAX_EXEC_ID_LENGTH for exec_id in ids)
