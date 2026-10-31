from decimal import Decimal

from ib_async import AccountValue

from app.brokers.ibkr.mapper import map_account_summary


def _av(tag, value):
    return AccountValue(account="DU1", tag=tag, value=value, currency="USD", modelCode="")


def test_account_facts_are_mapped():
    summary = map_account_summary([_av("NetLiquidation", "100"), _av("SettledCash", "80.25"), _av("DayTradesRemaining", "-1")])
    assert summary.settled_cash == Decimal("80.25")
    assert summary.day_trades_remaining == -1
    assert summary.available_funds is None


def test_unparseable_account_facts_are_none():
    summary = map_account_summary([_av("NetLiquidation", "100"), _av("SettledCash", "abc")])
    assert summary.settled_cash is None
    assert summary.day_trades_remaining is None


def test_price_bar_wap_set_only_when_positive():
    import datetime as dt

    from ib_async import BarData

    from app.brokers.ibkr.mapper import map_price_bar

    when = dt.datetime(2026, 10, 19, 14, 0, tzinfo=dt.UTC)
    bar = BarData(date=when, open=1, high=2, low=1, close=2, volume=10, average=1.5)
    assert map_price_bar(bar, "5 mins").wap == Decimal("1.5")
    bar.average = -1
    assert map_price_bar(bar, "5 mins").wap is None


async def test_ibkr_intraday_bars_drop_the_forming_bar():
    import datetime as dt
    from types import SimpleNamespace

    from ib_async import BarData

    from app.brokers.ibkr.client import IBKRClient
    from app.config import Settings

    now = dt.datetime.now(dt.UTC).replace(second=0, microsecond=0)
    done, forming = now - dt.timedelta(minutes=10), now - dt.timedelta(minutes=2)

    async def fake_history(contract, **kwargs):
        assert kwargs["barSizeSetting"] == "5 mins" and kwargs["formatDate"] == 2 and kwargs["useRTH"]
        return [BarData(date=d, open=1, high=1, low=1, close=1, volume=5, average=1) for d in (done, forming)]

    client = IBKRClient(Settings(
        database_url="postgresql+asyncpg://u:p@localhost/db", redis_url="redis://localhost:6379/0",
        broker="mock", environment="development",
    ))
    client._ib = SimpleNamespace(isConnected=lambda: True, reqHistoricalDataAsync=fake_history)

    bars = await client.get_intraday_bars("SCHB")
    assert [b.timestamp for b in bars] == [done]
