"""IBKR order mapping, order_ref lookup, execution summaries and cancel-on-timeout (C2, C3)."""

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.brokers import base
from app.brokers.ibkr import client as client_mod
from app.brokers.ibkr import mapper
from app.brokers.ibkr.client import IBKRClient
from app.config import Settings

TID = uuid.uuid4()


def _trade(status, filled=0, avg=0.0, ref="", order_id=7, total=10):
    return SimpleNamespace(
        order=SimpleNamespace(orderId=order_id, orderRef=ref, totalQuantity=total),
        orderStatus=SimpleNamespace(status=status, filled=filled, avgFillPrice=avg),
        log=[],
        done=status in ("Filled", "Cancelled", "ApiCancelled", "Inactive"),
        isDone=lambda: False,
    )


@pytest.mark.parametrize(
    ("status", "filled", "expected"),
    [
        ("Filled", 10, "FILLED"),
        ("Cancelled", 0, "REJECTED"),
        ("ApiCancelled", 0, "REJECTED"),
        ("Inactive", 0, "REJECTED"),
        ("Cancelled", 4, "PARTIAL"),
        ("Inactive", 4, "PARTIAL"),
        ("PendingSubmit", 0, "WORKING"),
        ("PreSubmitted", 0, "WORKING"),
        ("Submitted", 0, "WORKING"),
        ("ApiPending", 0, "WORKING"),
        ("PendingCancel", 0, "WORKING"),
        ("Submitted", 4, "WORKING"),
        ("PendingCancel", 4, "WORKING"),
        ("SomethingNew", 0, "ERROR"),
    ],
)
def test_map_order_result_statuses(status, filled, expected):
    result = mapper.map_order_result(TID, _trade(status, filled, 100.5 if filled else 0, f"{TID}:entry"))
    assert result.status == expected
    assert result.filled_quantity == Decimal(str(filled))
    assert result.order_ref == f"{TID}:entry"


def _fill(ref, shares, price, order_id=5):
    return SimpleNamespace(
        execution=SimpleNamespace(orderRef=ref, shares=shares, price=price, orderId=order_id)
    )


def _commission_fill(commission):
    return SimpleNamespace(commissionReport=SimpleNamespace(commission=commission))


def test_fills_commission_ignores_unset_and_zero():
    fills = [_commission_fill(0.36), _commission_fill(0.0), _commission_fill(1.7976931348623157e308)]
    assert mapper.fills_commission(fills) == Decimal("0.36")


def test_summarize_fills_carries_commission():
    ref = f"{TID}:exit"
    fill = _fill(ref, 2, 25.0)
    fill.commissionReport = SimpleNamespace(commission=0.36)
    assert mapper.summarize_fills(ref, [fill]).commission == Decimal("0.36")


def test_summarize_fills_volume_weighted():
    ref = f"{TID}:exit"
    fills = [_fill(ref, 4, 10.0), _fill(ref, 6, 20.0), _fill("other:exit", 99, 1.0)]
    result = mapper.summarize_fills(ref, fills)
    assert result.status == "FILLED"
    assert result.filled_quantity == Decimal("10")
    assert result.avg_fill_price == Decimal("16")
    assert result.trade_id == TID
    assert mapper.summarize_fills(f"{TID}:stop", fills) is None


class _FakeIB:
    def __init__(self, trade_obj=None):
        self.trade_obj = trade_obj
        self.placed = []
        self.cancelled = []
        self._trades = []
        self.fills = []
        self.open_orders_requested = 0

    def isConnected(self):
        return True

    def placeOrder(self, contract, order):
        self.placed.append(order)
        self._trades.append(self.trade_obj)
        return self.trade_obj

    def cancelOrder(self, order):
        self.cancelled.append(order)
        self.trade_obj.cancel_hook()

    def trades(self):
        return list(self._trades)

    async def reqAllOpenOrdersAsync(self):
        self.open_orders_requested += 1
        return []

    async def reqExecutionsAsync(self):
        return self.fills


class _LiveTrade:
    """Fake ib_async Trade whose state is driven by the test."""

    def __init__(self, status="Submitted", ref=""):
        self.order = SimpleNamespace(orderId=11, orderRef=ref, totalQuantity=10)
        self.orderStatus = SimpleNamespace(status=status, filled=0, avgFillPrice=0.0)
        self.log = []
        self.fills = []
        self.cancel_to = "Cancelled"

    def isDone(self):
        return self.orderStatus.status in ("Filled", "Cancelled", "ApiCancelled", "Inactive")

    def cancel_hook(self):
        self.orderStatus.status = self.cancel_to


def test_submitted_with_partial_fill_is_working_not_terminal():
    result = mapper.map_order_result(TID, _trade("Submitted", 4, 100.5, f"{TID}:entry"))
    assert result.status == "WORKING"
    assert result.filled_quantity == Decimal("4")
    assert result.ordered_quantity == Decimal("10")
    assert result.is_terminal is False


def test_pending_cancel_with_fills_is_working():
    result = mapper.map_order_result(TID, _trade("PendingCancel", 4, 100.5, f"{TID}:entry"))
    assert result.status == "WORKING"


def test_cancelled_with_fills_is_partial_and_terminal():
    result = mapper.map_order_result(TID, _trade("Cancelled", 4, 100.5, f"{TID}:entry"))
    assert result.status == "PARTIAL"
    assert result.is_terminal is True


def test_cancelled_without_fills_is_rejected_and_filled_is_filled():
    assert mapper.map_order_result(TID, _trade("Cancelled", 0)).status == "REJECTED"
    assert mapper.map_order_result(TID, _trade("Filled", 10, 100.0)).status == "FILLED"


def _execution_fill(side, commission, order_ref=f"{TID}:entry"):
    return SimpleNamespace(
        execution=SimpleNamespace(
            execId="0001.abc",
            orderRef=order_ref,
            orderId=5,
            side=side,
            shares=4,
            price=100.25,
            time=datetime(2026, 11, 13, 14, 30, tzinfo=UTC),
        ),
        commissionReport=SimpleNamespace(commission=commission),
        time=datetime(2026, 11, 13, 14, 30, 1, tzinfo=UTC),
    )


def test_map_executions_maps_side_ids_and_commission():
    fills = [_execution_fill("BOT", 0.36), _execution_fill("SLD", 1.7976931348623157e308)]
    executions = mapper.map_executions(fills)
    assert [e.side for e in executions] == ["BUY", "SELL"]
    assert executions[0].exec_id == "0001.abc"
    assert executions[0].order_ref == f"{TID}:entry"
    assert executions[0].broker_order_id == "5"
    assert executions[0].quantity == Decimal("4")
    assert executions[0].price == Decimal("100.25")
    assert executions[0].commission == Decimal("0.36")
    assert executions[0].executed_at.tzinfo is not None
    assert executions[1].commission is None


def _client(fake_ib, timeout=0):
    settings = Settings(
        database_url="postgresql+asyncpg://u:p@localhost/db",
        redis_url="redis://localhost:6379/0",
        broker="mock",
        environment="development",
        order_fill_timeout_seconds=timeout,
    )
    c = IBKRClient(settings)
    c._ib = fake_ib
    return c


def _req(**kw):
    d = {
        "trade_id": TID,
        "symbol": "AAPL",
        "direction": "BUY",
        "quantity": Decimal("10"),
        "order_ref": f"{TID}:entry",
    }
    d.update(kw)
    return base.OrderRequest(**d)


async def test_place_order_cancels_working_order_after_timeout():
    live = _LiveTrade("Submitted", f"{TID}:entry")
    ib = _FakeIB(live)
    result = await _client(ib).place_order(_req())
    assert len(ib.cancelled) == 1
    assert result.status == "REJECTED"
    assert ib.placed[0].orderRef == f"{TID}:entry"
    assert ib.placed[0].tif == "DAY"


async def test_place_order_timeout_with_partial_fill_maps_partial():
    live = _LiveTrade("Submitted", f"{TID}:entry")
    live.orderStatus.filled = 4
    live.orderStatus.avgFillPrice = 100.0
    ib = _FakeIB(live)
    result = await _client(ib).place_order(_req())
    assert result.status == "PARTIAL"
    assert result.filled_quantity == Decimal("4")


async def test_place_order_cancel_not_confirmed_stays_working(monkeypatch):
    monkeypatch.setattr(client_mod, "_CANCEL_WAIT_SECONDS", 0.2)
    live = _LiveTrade("Submitted", f"{TID}:entry")
    live.cancel_to = "PendingCancel"
    ib = _FakeIB(live)
    result = await _client(ib).place_order(_req())
    assert result.status == "WORKING"


async def test_stop_order_waits_only_for_ack_and_is_not_cancelled():
    live = _LiveTrade("PreSubmitted", f"{TID}:stop")
    ib = _FakeIB(live)
    result = await _client(ib, timeout=30).place_order(
        _req(order_type="STP", stop_price=Decimal("95"), tif="GTC", direction="SELL",
             order_ref=f"{TID}:stop")
    )
    assert result.status == "WORKING"
    assert ib.cancelled == []
    assert ib.placed[0].auxPrice == 95.0
    assert ib.placed[0].tif == "GTC"


async def test_find_order_by_ref_returns_latest_session_trade():
    ref = f"{TID}:exit"
    first, second = _LiveTrade("Cancelled", ref), _LiveTrade("Submitted", ref)
    ib = _FakeIB()
    ib._trades = [first, second]
    result = await _client(ib).find_order_by_ref(ref)
    assert result.status == "WORKING"
    assert ib.open_orders_requested == 0


async def test_find_order_by_ref_prefers_live_order_over_later_terminal_one():
    """After a reconnect the cancelled old stop can arrive after its live replacement."""
    ref = f"{TID}:stop"
    live, old = _LiveTrade("Submitted", ref), _LiveTrade("Cancelled", ref)
    live.order.orderId, old.order.orderId = 12, 11
    ib = _FakeIB()
    ib._trades = [live, old]
    result = await _client(ib).find_order_by_ref(ref)
    assert result.status == "WORKING"
    assert result.broker_order_id == "12"


async def test_find_order_by_ref_none_when_unknown():
    ib = _FakeIB()
    c = _client(ib)
    assert await c.find_order_by_ref(f"{TID}:entry") is None
    assert ib.open_orders_requested == 1


async def test_get_execution_summary_aggregates():
    ref = f"{TID}:exit"
    ib = _FakeIB()
    ib.fills = [_fill(ref, 3, 50.0), _fill(ref, 7, 60.0)]
    result = await _client(ib).get_execution_summary(ref)
    assert result.filled_quantity == Decimal("10")
    assert result.avg_fill_price == Decimal("57")


async def test_get_executions_keeps_commission_reported_on_cached_fill():
    from ib_async import CommissionReport, Execution, Fill

    def make_fill(commission):
        execution = Execution(
            execId="0001.1", orderRef=f"{TID}:entry", orderId=7, side="BOT",
            shares=10.0, price=10.0, time=datetime(2026, 1, 5, 15, 0, tzinfo=UTC),
        )
        report = CommissionReport(execId="0001.1", commission=commission)
        return Fill(SimpleNamespace(), execution, report, execution.time)

    ib = _FakeIB()
    ib.fills = [make_fill(0.0)]
    ib.wrapper = SimpleNamespace(fills={"0001.1": make_fill(1.0)})
    [execution] = await _client(ib).get_executions()
    assert execution.commission == Decimal("1.0")

    ib.wrapper = SimpleNamespace(fills={})
    [execution] = await _client(ib).get_executions()
    assert execution.commission is None
