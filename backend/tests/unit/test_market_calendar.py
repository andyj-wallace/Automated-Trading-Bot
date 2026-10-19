from datetime import UTC, date, datetime, time

from app.core import market_calendar as mc

ET = mc.ET


def _et(y, m, d, hh, mm):
    return datetime(y, m, d, hh, mm, tzinfo=ET)


def test_thanksgiving_not_trading_day():
    assert not mc.is_trading_day(date(2026, 11, 26))
    assert mc.is_trading_day(date(2026, 11, 25))


def test_day_after_thanksgiving_early_close():
    bounds = mc.session_bounds(date(2026, 11, 27))
    assert bounds is not None
    assert bounds[1] == datetime(2026, 11, 27, 18, 0, tzinfo=UTC)


def test_session_bounds_none_on_holiday():
    assert mc.session_bounds(date(2026, 11, 26)) is None


def test_market_open_boundaries():
    assert mc.is_market_open(_et(2026, 10, 19, 10, 0))
    assert mc.is_market_open(_et(2026, 10, 19, 9, 30))
    assert not mc.is_market_open(_et(2026, 10, 19, 9, 29))
    assert not mc.is_market_open(_et(2026, 10, 19, 16, 0))


def test_saturday_closed():
    assert not mc.is_market_open(_et(2026, 10, 17, 11, 0))


def test_previous_trading_day_skips_weekend():
    assert mc.previous_trading_day(date(2026, 10, 19)) == date(2026, 10, 16)


def test_in_session_window():
    start, end = time(9, 45), time(15, 45)
    assert mc.in_session_window(start, end, _et(2026, 10, 19, 10, 0))
    assert not mc.in_session_window(start, end, _et(2026, 10, 19, 9, 40))
    assert not mc.in_session_window(start, end, _et(2026, 10, 19, 15, 45))
    assert not mc.in_session_window(start, end, _et(2026, 11, 27, 13, 30))


def test_trading_date_uses_et():
    utc_late = datetime(2026, 10, 20, 2, 0, tzinfo=UTC)
    assert mc.trading_date(utc_late) == date(2026, 10, 19)
