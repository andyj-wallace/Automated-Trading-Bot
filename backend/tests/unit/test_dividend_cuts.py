from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from app.core.dividend_cuts import CutStatus, detect_cut


@dataclass
class Ev:
    ex_dividend_date: date
    amount: Decimal


def D(s: str) -> Decimal:
    return Decimal(s)


Q = [date(2025, 11, 14), date(2026, 2, 13), date(2026, 5, 15), date(2026, 8, 14)]
N1 = date(2026, 11, 13)
TODAY = date(2026, 8, 20)


def series(amounts: list[str], dates: list[date]) -> list[Ev]:
    return [Ev(d, D(a)) for d, a in zip(dates, amounts, strict=True)]


def x3_then(last: str) -> list[Ev]:
    return series(["1.00", "1.00", "1.00", last], Q)


def test_real_cut_070():
    assert detect_cut(x3_then("0.70"), TODAY) == CutStatus.CUT


def test_real_cut_060():
    assert detect_cut(x3_then("0.60"), TODAY) == CutStatus.CUT


def test_unchanged_is_none():
    assert detect_cut(series(["1.00"] * 4, Q), TODAY) == CutStatus.NONE


def test_increase_is_none():
    assert detect_cut(x3_then("1.10"), TODAY) == CutStatus.NONE


def test_split_half_is_none():
    assert detect_cut(x3_then("0.50"), TODAY) == CutStatus.NONE


def test_split_off_by_0_0004_is_none():
    assert detect_cut(x3_then("0.5002"), TODAY) == CutStatus.NONE


def test_split_third_is_none():
    assert detect_cut(x3_then("0.3333"), TODAY) == CutStatus.NONE


def test_split_three_for_one_on_090_is_none():
    assert detect_cut(series(["0.90"] * 3 + ["0.30"], Q), TODAY) == CutStatus.NONE


def test_quarterly_to_monthly_equal_total_is_none():
    events = series(["0.90"] * 4, Q) + [Ev(date(2026, 9, 15), D("0.30"))]
    assert detect_cut(events, date(2026, 9, 20)) == CutStatus.NONE


def test_quarterly_to_monthly_full_year_is_none():
    monthly = [
        date(2026, 9, 15),
        date(2026, 10, 15),
        date(2026, 11, 15),
        date(2026, 12, 15),
        date(2027, 1, 15),
        date(2027, 2, 15),
    ]
    events = series(["0.90"] * 4, Q) + [Ev(d, D("0.30")) for d in monthly]
    assert detect_cut(events, date(2027, 2, 20)) == CutStatus.NONE


def test_quarterly_to_semiannual_equal_total_is_none():
    events = series(
        ["0.50", "0.50", "0.50", "0.50"],
        [
            date(2025, 2, 14),
            date(2025, 5, 15),
            date(2025, 8, 14),
            date(2025, 11, 14),
        ],
    ) + [Ev(date(2026, 5, 15), D("1.00"))]
    assert detect_cut(events, date(2026, 5, 20)) == CutStatus.NONE


def test_annualised_cut_on_frequency_change():
    events = series(["1.00"] * 4, Q) + [Ev(date(2026, 9, 15), D("0.30"))]
    assert detect_cut(events, date(2026, 9, 20)) == CutStatus.CUT


def test_unmapped_gap_is_none_within_window():
    events = x3_then("0.70")
    events[-1] = Ev(date(2026, 7, 4), D("0.70"))
    assert detect_cut(events, date(2026, 7, 10)) == CutStatus.NONE


def test_unmapped_gap_does_not_fall_through_to_omission():
    events = x3_then("0.70")
    events[-1] = Ev(date(2026, 7, 4), D("0.70"))
    assert detect_cut(events, date(2027, 3, 1)) == CutStatus.NONE


def test_split_like_drop_does_not_fall_through_to_omission():
    assert detect_cut(x3_then("0.50"), date(2027, 1, 15)) == CutStatus.NONE


def test_earlier_special_is_excluded():
    events = [
        Ev(date(2025, 5, 15), D("1.00")),
        Ev(date(2025, 8, 14), D("1.00")),
        Ev(date(2025, 11, 14), D("1.00")),
        Ev(date(2026, 2, 13), D("1.00")),
        Ev(date(2026, 4, 15), D("3.00")),
        Ev(date(2026, 5, 15), D("1.00")),
        Ev(date(2026, 8, 14), D("1.00")),
    ]
    assert detect_cut(events, TODAY) == CutStatus.NONE


def test_special_then_normal_payment_is_none():
    events = series(["1.00"] * 4, Q) + [
        Ev(date(2026, 9, 10), D("2.80")),
        Ev(date(2026, 11, 13), D("1.00")),
    ]
    assert detect_cut(events, date(2026, 11, 20)) == CutStatus.NONE


def test_omission_after_grace():
    assert detect_cut(series(["1.00"] * 4, Q), date(2027, 1, 15)) == CutStatus.OMISSION


def test_omission_inside_grace_is_none():
    assert detect_cut(series(["1.00"] * 4, Q), date(2026, 12, 1)) == CutStatus.NONE


def test_cut_wins_over_omission():
    assert detect_cut(x3_then("0.60"), date(2026, 12, 28)) == CutStatus.CUT


def test_cut_wins_over_omission_later():
    assert detect_cut(x3_then("0.60"), date(2027, 1, 15)) == CutStatus.CUT


def test_declared_future_lower_dividend_is_ignored():
    events = series(["1.00"] * 4, Q) + [Ev(N1, D("0.50"))]
    assert detect_cut(events, TODAY) == CutStatus.NONE


def test_fewer_than_four_events_is_none():
    events = series(["1.00", "1.00", "0.70"], Q[:3])
    assert detect_cut(events, TODAY) == CutStatus.NONE
