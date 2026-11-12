"""SQL shape of the current_stop_price writers in TradeRepo."""

import uuid
from decimal import Decimal

from sqlalchemy.dialects import postgresql

from app.db.repositories.trade_repo import TradeRepo


class _FakeSession:
    def __init__(self) -> None:
        self.stmt = None

    async def execute(self, stmt) -> None:
        self.stmt = stmt


def _sql(stmt) -> str:
    return str(stmt.compile(dialect=postgresql.dialect())).lower()


async def test_set_stop_order_uses_greatest():
    session = _FakeSession()
    await TradeRepo(session).set_stop_order(uuid.uuid4(), "S1", Decimal("195"))
    sql = _sql(session.stmt)
    assert "greatest(" in sql
    assert "current_stop_price" in sql


async def test_set_stop_order_without_price_leaves_stop():
    session = _FakeSession()
    await TradeRepo(session).set_stop_order(uuid.uuid4(), "S1", None)
    assert "current_stop_price" not in _sql(session.stmt)


async def test_raise_current_stop_is_conditional():
    session = _FakeSession()
    await TradeRepo(session).raise_current_stop(uuid.uuid4(), Decimal("201"))
    sql = _sql(session.stmt)
    assert "current_stop_price is null" in sql
    assert "current_stop_price <" in sql
    assert "stop_loss_price" not in sql
