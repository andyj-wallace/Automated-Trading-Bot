from decimal import Decimal as D

from app.core.risk.costs import PLANS, CostModel, breakeven_win_rate

FIXED = CostModel(PLANS["ibkr_fixed"], D("5"))
TIERED = CostModel(PLANS["ibkr_tiered"], D("5"))


def test_fixed_commission():
    assert FIXED.commission(D(2), D(25)) == D("0.50")  # 1% cap beats the $1 minimum
    assert FIXED.commission(D(100), D(25)) == D("1.00")
    assert FIXED.commission(D(400), D(25)) == D("2.00")


def test_tiered_commission():
    assert TIERED.commission(D(2), D(25)) == D("0.36")
    assert TIERED.commission(D(1), D(20)) == D("0.20")
    assert TIERED.commission(D(20), D(25)) == D("0.42")
    assert TIERED.commission(D(0), D(25)) == D("0")


def test_round_trip_cost():
    assert TIERED.round_trip_cost(D(2), D(25)) == D("0.77")
    assert FIXED.round_trip_cost(D(2), D(25)) == D("1.05")


def test_breakeven_win_rate():
    assert breakeven_win_rate(D(1), D(2), D(0)) == D(1) / D(3)
    assert breakeven_win_rate(D(1), D(2), D("0.77")) > D("0.45")
