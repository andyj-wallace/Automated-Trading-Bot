from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from app.core.dividends import (
    forward_yield,
    frequency,
    gap_frequency,
    gaps,
    paid_events,
    regular_events,
    trailing_yield,
)


@dataclass
class Ev:
    ex_dividend_date: date
    amount: Decimal
    payment_date: date | None = None


def ev(d: str, amount: str, payment_date: date | None = None) -> Ev:
    return Ev(date.fromisoformat(d), Decimal(amount), payment_date)


TODAY = date(2026, 11, 1)
Q = [
    ev("2025-11-14", "0.50"),
    ev("2026-02-13", "0.50"),
    ev("2026-05-15", "0.50"),
    ev("2026-08-14", "0.50"),
]


def test_quarterly_payer():
    events = Q
    assert frequency(events) == 4
    assert trailing_yield(events, Decimal("100"), TODAY) == Decimal("0.02")
    assert forward_yield(events, Decimal("100"), TODAY) == Decimal("0.02")


def test_monthly_payer():
    months = [(2025, 11), (2025, 12), *((2026, m) for m in range(1, 11))]
    events = [ev(f"{y}-{m:02d}-15", "0.10") for y, m in months]
    assert len(events) == 12
    assert frequency(events) == 12
    assert trailing_yield(events, Decimal("40"), TODAY) == Decimal("0.03")
    assert forward_yield(events, Decimal("40"), TODAY) == Decimal("0.03")


def test_special_dividend_excluded_from_yields():
    events = [ev("2025-08-15", "0.50"), *Q, ev("2026-07-01", "3.00")]
    events.sort(key=lambda e: e.ex_dividend_date)
    regs = regular_events(events)
    assert len(regs) == 5
    assert all(e.amount == Decimal("0.50") for e in regs)
    assert frequency(events) == 4
    assert trailing_yield(events, Decimal("100"), TODAY) == Decimal("0.02")
    assert forward_yield(events, Decimal("100"), TODAY) == Decimal("0.02")


def test_latest_high_event_kept_known_limit():
    events = [*Q, ev("2026-09-03", "3.00")]
    regs = regular_events(events)
    assert len(regs) == 5
    assert regs[-1].amount == Decimal("3.00")


def test_lasting_raise_across_frequency_change():
    events = [
        ev("2026-01-01", "1.00"),
        ev("2026-01-31", "1.00"),
        ev("2026-03-02", "1.00"),
        ev("2026-04-01", "1.00"),
        ev("2026-06-30", "3.00"),
        ev("2026-09-28", "3.00"),
    ]
    assert len(regular_events(events)) == 6
    assert gaps(events) == [30, 30, 30, 90, 90]
    assert frequency(events) == 4
    assert forward_yield(events, Decimal("100"), TODAY) == Decimal("0.12")
    assert trailing_yield(events, Decimal("100"), TODAY) == Decimal("0.1")


def test_price_zero_returns_none():
    assert trailing_yield(Q, Decimal("0"), TODAY) is None
    assert forward_yield(Q, Decimal("0"), TODAY) is None


def test_fewer_than_four_events_returns_none():
    first_three = Q[:3]
    assert trailing_yield(first_three, Decimal("100"), TODAY) is None
    assert forward_yield(first_three, Decimal("100"), TODAY) is None
    assert frequency(first_three) is None


def test_ex_date_passed_payment_date_future_or_null_still_counted():
    base = [
        ev("2026-01-30", "0.50"),
        ev("2026-04-30", "0.50"),
        ev("2026-07-30", "0.50"),
    ]
    for last_payment in (date(2026, 11, 20), None):
        events = [*base, ev("2026-10-30", "0.50", last_payment)]
        assert trailing_yield(events, Decimal("100"), TODAY) == Decimal("0.02")
        assert forward_yield(events, Decimal("100"), TODAY) == Decimal("0.02")


def test_declared_future_dividend_ignored():
    events = [*Q, ev("2026-11-13", "0.60")]
    assert trailing_yield(events, Decimal("100"), TODAY) == Decimal("0.02")
    assert forward_yield(events, Decimal("100"), TODAY) == Decimal("0.02")


def test_gap_sequence_quarterly():
    events = _five_events_with_gaps([30, 30, 90, 90])
    assert frequency(events) == 4


def test_gap_sequence_monthly():
    events = _five_events_with_gaps([90, 90, 30, 30])
    assert frequency(events) == 12


def _five_events_with_gaps(day_gaps: list[int]) -> list[Ev]:
    d = date(2025, 1, 1)
    out = [Ev(d, Decimal("0.50"))]
    for g in day_gaps:
        d = d + timedelta(days=g)
        out.append(Ev(d, Decimal("0.50")))
    return out


def test_quarterly_to_monthly_switch_not_special():
    quarterly = [Ev(e.ex_dividend_date, Decimal("0.90")) for e in Q]
    events = [
        *quarterly,
        ev("2026-09-15", "0.30"),
        ev("2026-10-15", "0.30"),
        ev("2026-11-15", "0.30"),
    ]
    today = date(2026, 11, 20)
    paid = paid_events(events, today)
    assert frequency(paid) == 12
    assert len(regular_events(paid)) == 7
    assert forward_yield(events, Decimal("100"), today) == Decimal("0.036")
    assert trailing_yield(events, Decimal("100"), today) == Decimal("0.036")


def test_gap_frequency_mapping():
    assert gap_frequency(30) == 12
    assert gap_frequency(91) == 4
    assert gap_frequency(182) == 2
    assert gap_frequency(365) == 1
    assert gap_frequency(50) is None
