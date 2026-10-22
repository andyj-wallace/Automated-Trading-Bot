"""Operator hint for an unreachable IB Gateway."""

from datetime import datetime, time

import pytest

from app.brokers.ibkr.hints import DAILY_RESTART, GENERAL, WEEKLY_LOGIN, login_hint
from app.core.market_calendar import ET

RESTART = time(19, 0)


@pytest.mark.parametrize(
    ("when", "expected"),
    [
        (datetime(2026, 10, 25, 19, 5, tzinfo=ET), WEEKLY_LOGIN),  # Sunday, just after the restart
        (datetime(2026, 10, 25, 20, 30, tzinfo=ET), GENERAL),  # Sunday, long after it
        (datetime(2026, 10, 25, 18, 55, tzinfo=ET), GENERAL),  # Sunday, before it
        (datetime(2026, 10, 21, 19, 3, tzinfo=ET), DAILY_RESTART),  # Wednesday restart
        (datetime(2026, 10, 21, 19, 20, tzinfo=ET), GENERAL),
        (datetime(2026, 10, 21, 11, 0, tzinfo=ET), GENERAL),
    ],
)
def test_login_hint(when, expected):
    assert login_hint(when, RESTART) == expected


def test_hints_lead_with_the_action():
    # SMS alerts keep only the first 100 characters of the message.
    for hint in (WEEKLY_LOGIN, GENERAL):
        assert "IBKR Mobile" in f"IB Gateway down 5 min. {hint}"[:100]
