"""Unit tests for CircuitBreaker (daily loss halt + per-strategy kill switch)."""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.core.risk.circuit_breaker import CircuitBreaker

_DAY_1 = datetime(2025, 1, 1, 10, 0, tzinfo=UTC)
_DAY_2 = _DAY_1 + timedelta(days=1)


# ---------------------------------------------------------------------------
# Daily halt
# ---------------------------------------------------------------------------


def test_not_halted_initially() -> None:
    cb = CircuitBreaker()
    assert cb.is_halted() is False


def test_halts_once_daily_loss_limit_reached() -> None:
    cb = CircuitBreaker(daily_loss_limit_pct=Decimal("0.03"))
    balance = Decimal("100000")

    cb.record_closed_trade(Decimal("-2000"), balance, now=_DAY_1)
    assert cb.is_halted(now=_DAY_1) is False  # 2% loss, under 3% limit

    cb.record_closed_trade(Decimal("-1000"), balance, now=_DAY_1)
    assert cb.is_halted(now=_DAY_1) is True  # cumulative 3% loss trips it


def test_winning_trades_do_not_trip_halt() -> None:
    cb = CircuitBreaker(daily_loss_limit_pct=Decimal("0.03"))
    balance = Decimal("100000")

    cb.record_closed_trade(Decimal("5000"), balance, now=_DAY_1)
    cb.record_closed_trade(Decimal("-1000"), balance, now=_DAY_1)
    assert cb.is_halted() is False


def test_halt_resets_on_new_calendar_day() -> None:
    cb = CircuitBreaker(daily_loss_limit_pct=Decimal("0.03"))
    balance = Decimal("100000")

    cb.record_closed_trade(Decimal("-5000"), balance, now=_DAY_1)
    assert cb.is_halted(now=_DAY_1) is True

    cb.record_closed_trade(Decimal("100"), balance, now=_DAY_2)
    assert cb.is_halted(now=_DAY_2) is False
    assert cb.daily_realized_pnl == Decimal("100")


def test_admin_reset_lifts_halt_without_day_rollover() -> None:
    cb = CircuitBreaker(daily_loss_limit_pct=Decimal("0.03"))
    balance = Decimal("100000")

    cb.record_closed_trade(Decimal("-5000"), balance, now=_DAY_1)
    assert cb.is_halted(now=_DAY_1) is True

    cb.reset_daily_halt()
    assert cb.is_halted(now=_DAY_1) is False


def test_halt_does_not_outlive_its_trading_day_without_new_trades() -> None:
    cb = CircuitBreaker(daily_loss_limit_pct=Decimal("0.03"))
    cb.record_closed_trade(Decimal("-5000"), Decimal("100000"), now=_DAY_1)
    assert cb.is_halted(now=_DAY_1) is True
    assert cb.is_halted(now=_DAY_2) is False


def test_trading_day_rolls_at_et_midnight_not_utc() -> None:
    cb = CircuitBreaker(daily_loss_limit_pct=Decimal("0.03"))
    evening_et = datetime(2025, 1, 2, 1, 0, tzinfo=UTC)  # 20:00 ET on Jan 1
    cb.record_closed_trade(Decimal("-5000"), Decimal("100000"), now=_DAY_1)
    cb.record_closed_trade(Decimal("-10"), Decimal("95000"), now=evening_et)
    # 20:00 ET is still the Jan 1 trading day: the halt stands and the balance is unchanged
    assert cb.is_halted(now=evening_et) is True
    assert cb.day_starting_balance == Decimal("100000")


def test_record_returns_events_only_when_first_triggered() -> None:
    sid = uuid.uuid4()
    cb = CircuitBreaker(daily_loss_limit_pct=Decimal("0.03"), max_consecutive_losses=2)
    balance = Decimal("100000")
    assert cb.record_closed_trade(Decimal("-100"), balance, sid, now=_DAY_1) == []
    assert cb.record_closed_trade(Decimal("-100"), balance, sid, now=_DAY_1) == ["STRATEGY_KILLED"]
    assert cb.record_closed_trade(Decimal("-100"), balance, sid, now=_DAY_1) == []
    assert cb.record_closed_trade(Decimal("-5000"), balance, None, now=_DAY_1) == ["DAILY_HALT"]
    assert cb.record_closed_trade(Decimal("-5000"), balance, None, now=_DAY_1) == []


def test_rehydrate_restores_halt_and_streaks() -> None:
    losing, winning = uuid.uuid4(), uuid.uuid4()
    cb = CircuitBreaker(daily_loss_limit_pct=Decimal("0.03"), max_consecutive_losses=3)
    cb.rehydrate(
        realized_pnl_today=Decimal("-3000"),
        day_starting_balance=Decimal("100000"),
        recent_pnls_by_strategy={
            losing: [Decimal("-1"), Decimal("-2"), Decimal("-3"), Decimal("5")],
            winning: [Decimal("-1"), Decimal("4"), Decimal("-9")],
        },
        now=_DAY_1,
    )
    assert cb.is_halted(now=_DAY_1) is True
    assert cb.is_halted(now=_DAY_2) is False
    # Kills are durable via strategies.is_enabled, not re-derived here.
    assert cb.consecutive_losses(losing) == 3 and cb.is_strategy_killed(losing) is False
    assert cb.consecutive_losses(winning) == 1 and cb.is_strategy_killed(winning) is False


def test_rehydrate_below_threshold_is_not_halted() -> None:
    cb = CircuitBreaker(daily_loss_limit_pct=Decimal("0.03"))
    cb.rehydrate(
        realized_pnl_today=Decimal("-2999"),
        day_starting_balance=Decimal("100000"),
        recent_pnls_by_strategy={},
        now=_DAY_1,
    )
    assert cb.is_halted(now=_DAY_1) is False


def test_day_starting_balance_set_from_first_trade_of_day() -> None:
    cb = CircuitBreaker()
    cb.record_closed_trade(Decimal("-100"), Decimal("50000"), now=_DAY_1)
    assert cb.day_starting_balance == Decimal("50000")

    # A later trade the same day does not reset the day's starting balance,
    # even if it reports a different balance figure.
    cb.record_closed_trade(Decimal("-100"), Decimal("49000"), now=_DAY_1 + timedelta(hours=1))
    assert cb.day_starting_balance == Decimal("50000")


# ---------------------------------------------------------------------------
# Per-strategy kill switch
# ---------------------------------------------------------------------------


def test_strategy_not_killed_initially() -> None:
    cb = CircuitBreaker()
    assert cb.is_strategy_killed(uuid.uuid4()) is False


def test_strategy_with_no_id_is_never_killed() -> None:
    cb = CircuitBreaker(max_consecutive_losses=1)
    cb.record_closed_trade(Decimal("-100"), Decimal("100000"), strategy_id=None)
    assert cb.is_strategy_killed(None) is False


def test_strategy_killed_after_max_consecutive_losses() -> None:
    cb = CircuitBreaker(max_consecutive_losses=3)
    sid = uuid.uuid4()
    balance = Decimal("100000")

    cb.record_closed_trade(Decimal("-10"), balance, strategy_id=sid)
    assert cb.is_strategy_killed(sid) is False
    cb.record_closed_trade(Decimal("-10"), balance, strategy_id=sid)
    assert cb.is_strategy_killed(sid) is False
    cb.record_closed_trade(Decimal("-10"), balance, strategy_id=sid)
    assert cb.is_strategy_killed(sid) is True


def test_win_resets_consecutive_loss_streak() -> None:
    cb = CircuitBreaker(max_consecutive_losses=3)
    sid = uuid.uuid4()
    balance = Decimal("100000")

    cb.record_closed_trade(Decimal("-10"), balance, strategy_id=sid)
    cb.record_closed_trade(Decimal("-10"), balance, strategy_id=sid)
    cb.record_closed_trade(Decimal("50"), balance, strategy_id=sid)  # win resets streak
    assert cb.consecutive_losses(sid) == 0

    cb.record_closed_trade(Decimal("-10"), balance, strategy_id=sid)
    cb.record_closed_trade(Decimal("-10"), balance, strategy_id=sid)
    assert cb.is_strategy_killed(sid) is False  # only 2 in a row since the win


def test_killed_strategy_stays_killed_after_a_win() -> None:
    """Once killed, a strategy does not self-heal on a subsequent win."""
    cb = CircuitBreaker(max_consecutive_losses=2)
    sid = uuid.uuid4()
    balance = Decimal("100000")

    cb.record_closed_trade(Decimal("-10"), balance, strategy_id=sid)
    cb.record_closed_trade(Decimal("-10"), balance, strategy_id=sid)
    assert cb.is_strategy_killed(sid) is True

    cb.record_closed_trade(Decimal("50"), balance, strategy_id=sid)
    assert cb.is_strategy_killed(sid) is True


def test_killed_strategy_ids_lists_only_killed() -> None:
    cb = CircuitBreaker(max_consecutive_losses=1)
    sid_killed = uuid.uuid4()
    sid_fine = uuid.uuid4()
    balance = Decimal("100000")

    cb.record_closed_trade(Decimal("-10"), balance, strategy_id=sid_killed)
    cb.record_closed_trade(Decimal("10"), balance, strategy_id=sid_fine)

    assert cb.killed_strategy_ids() == [sid_killed]


def test_manual_kill_strategy() -> None:
    cb = CircuitBreaker()
    sid = uuid.uuid4()
    cb.kill_strategy(sid)
    assert cb.is_strategy_killed(sid) is True


def test_reset_strategy_re_enables_and_clears_streak() -> None:
    cb = CircuitBreaker(max_consecutive_losses=2)
    sid = uuid.uuid4()
    balance = Decimal("100000")

    cb.record_closed_trade(Decimal("-10"), balance, strategy_id=sid)
    cb.record_closed_trade(Decimal("-10"), balance, strategy_id=sid)
    assert cb.is_strategy_killed(sid) is True

    cb.reset_strategy(sid)
    assert cb.is_strategy_killed(sid) is False
    assert cb.consecutive_losses(sid) == 0
