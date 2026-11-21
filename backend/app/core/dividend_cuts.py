"""Detect a dividend cut or omission from the ex-date history (pure, no I/O)."""

from collections.abc import Sequence
from datetime import date, timedelta
from decimal import Decimal
from enum import StrEnum
from statistics import median

from app.core.dividends import DividendLike, gap_frequency, gaps, paid_events, regular_events

SPLIT_TOLERANCE = Decimal("0.0005")


class CutStatus(StrEnum):
    """Result of a dividend cut check."""

    CUT = "CUT"
    OMISSION = "OMISSION"
    NONE = "NONE"


def detect_cut(
    events: Sequence[DividendLike],
    today: date,
    grace_days: int = 45,
    tolerance: Decimal = Decimal("0.01"),
) -> CutStatus:
    """Return CUT, OMISSION or NONE for the dividend history as of today.

    Limits: no split data exists, so a drop to within 0.05% of 1/N (N 2..10) of the previous
    regular amount is treated as a split and returns NONE; a real cut to exactly 1/N is missed.
    ETFs are not identified. Future-dated events are ignored until their ex-date is reached.
    """
    p = paid_events(events, today)
    if len(p) < 4:
        return CutStatus.NONE
    regs = regular_events(p)
    g = gaps(regs)
    if len(g) < 2:
        return CutStatus.NONE

    prev, last = regs[-2], regs[-1]
    f_prev = gap_frequency(g[-2])
    f_last = gap_frequency(g[-1])
    if f_prev is None or f_last is None or prev.amount <= 0:
        return CutStatus.NONE

    ratio = last.amount / prev.amount
    if any(abs(ratio * n - 1) <= SPLIT_TOLERANCE for n in range(2, 11)):
        return CutStatus.NONE
    if last.amount * f_last < prev.amount * f_prev * (1 - tolerance):
        return CutStatus.CUT

    expected = regs[-1].ex_dividend_date + timedelta(days=median(g[-3:]))
    no_later_paid = not any(e.ex_dividend_date > regs[-1].ex_dividend_date for e in p)
    if no_later_paid and today > expected + timedelta(days=grace_days):
        return CutStatus.OMISSION
    return CutStatus.NONE
