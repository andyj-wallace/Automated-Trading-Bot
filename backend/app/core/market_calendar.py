"""NYSE calendar helpers. All datetimes are timezone-aware."""

from __future__ import annotations

from datetime import date, datetime, time
from functools import lru_cache
from zoneinfo import ZoneInfo

import exchange_calendars as xcals
import pandas as pd

ET = ZoneInfo("America/New_York")


@lru_cache(maxsize=2)
def _calendar(year: int):
    return xcals.get_calendar("XNYS")


def _cal():
    return _calendar(datetime.now(ET).year)


def now_et() -> datetime:
    """Current time in America/New_York."""
    return datetime.now(ET)


def trading_date(now: datetime | None = None) -> date:
    """The ET calendar date of `now` (default: current time)."""
    return (now or now_et()).astimezone(ET).date()


def is_trading_day(day: date) -> bool:
    """True if `day` is an NYSE session."""
    return bool(_cal().is_session(pd.Timestamp(day)))


def session_bounds(day: date) -> tuple[datetime, datetime] | None:
    """UTC-aware (open, close) for `day`, or None if not a trading day."""
    if not is_trading_day(day):
        return None
    ts = pd.Timestamp(day)
    return _cal().session_open(ts).to_pydatetime(), _cal().session_close(ts).to_pydatetime()


def is_market_open(now: datetime | None = None) -> bool:
    """True during regular NYSE hours (early closes handled)."""
    now = (now or now_et()).astimezone(ET)
    bounds = session_bounds(now.date())
    return bounds is not None and bounds[0] <= now < bounds[1]


def in_session_window(start: time, end: time, now: datetime | None = None) -> bool:
    """True if `now` is inside [start, end) ET and within the regular session."""
    now = (now or now_et()).astimezone(ET)
    bounds = session_bounds(now.date())
    if bounds is None:
        return False
    window_start = max(bounds[0], datetime.combine(now.date(), start, ET))
    window_end = min(bounds[1], datetime.combine(now.date(), end, ET))
    return window_start <= now < window_end


def previous_trading_day(day: date) -> date:
    """The last NYSE session strictly before `day`."""
    prior = pd.Timestamp(day) - pd.Timedelta(days=1)
    return _cal().date_to_session(prior, direction="previous").date()
