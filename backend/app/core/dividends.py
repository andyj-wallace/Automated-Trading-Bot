"""Dividend frequency, special-dividend filter and yield helpers (pure, no I/O)."""

from collections.abc import Sequence
from datetime import date, timedelta
from decimal import Decimal
from statistics import median
from typing import Protocol


class DividendLike(Protocol):
    """Any object with an ex-dividend date and a per-share amount."""

    ex_dividend_date: date
    amount: Decimal


def gap_frequency(gap_days: float) -> int | None:
    """Map a typical day gap between ex-dates to payments per year."""
    g = gap_days
    if 20 <= g <= 45:
        return 12
    if 60 <= g <= 120:
        return 4
    if 150 <= g <= 220:
        return 2
    if 300 <= g <= 430:
        return 1
    return None


def paid_events(events: Sequence[DividendLike], today: date) -> list[DividendLike]:
    """Events whose ex-date has been reached, ascending by ex-date."""
    return sorted(
        (e for e in events if e.ex_dividend_date <= today),
        key=lambda e: e.ex_dividend_date,
    )


def _is_special(s: list[DividendLike], i: int) -> bool:
    if not 4 <= i < len(s) - 1:
        return False
    prior = median(e.amount for e in s[i - 4 : i])
    return s[i].amount >= Decimal("2.5") * prior and abs(
        s[i + 1].amount - s[i].amount
    ) > s[i].amount * Decimal("0.05")


def regular_events(events: Sequence[DividendLike]) -> list[DividendLike]:
    """Ascending events with specials removed.

    An event is special when it is at least 2.5x the median of the 4 raw events before it and
    the next payment is not within 5% of it. The latest event is never special, so a special
    that is the latest event counts as regular until the next payment arrives (known limit).
    """
    s = sorted(events, key=lambda e: e.ex_dividend_date)
    special = [_is_special(s, i) for i in range(len(s))]
    return [e for e, sp in zip(s, special, strict=True) if not sp]


def gaps(regs: Sequence[DividendLike]) -> list[int]:
    """Day gaps between consecutive ex-dates of an ascending list."""
    return [(b.ex_dividend_date - a.ex_dividend_date).days for a, b in zip(regs, regs[1:], strict=False)]


def frequency(events: Sequence[DividendLike]) -> int | None:
    """Payments per year from ex-date gaps, not from the API; callers pass paid events.

    Uses the median of the 3 most recent gaps between regular events.
    """
    g = gaps(regular_events(events))
    if len(g) < 3:
        return None
    return gap_frequency(median(g[-3:]))


def trailing_yield(
    events: Sequence[DividendLike], price: Decimal, today: date
) -> Decimal | None:
    """Regular amounts paid in the last 365 days divided by price."""
    if price <= 0:
        return None
    p = paid_events(events, today)
    if len(p) < 4:
        return None
    regs = regular_events(p)
    window_start = today - timedelta(days=365)
    total = sum(
        (e.amount for e in regs if window_start < e.ex_dividend_date <= today), Decimal("0")
    )
    return total / price


def forward_yield(
    events: Sequence[DividendLike], price: Decimal, today: date
) -> Decimal | None:
    """Latest regular amount times payments per year, divided by price."""
    if price <= 0:
        return None
    p = paid_events(events, today)
    if len(p) < 4:
        return None
    freq = frequency(p)
    if freq is None:
        return None
    regs = regular_events(p)
    return regs[-1].amount * freq / price
