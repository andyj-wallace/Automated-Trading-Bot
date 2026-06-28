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
    assert cb.is_halted() is False  # 2% loss, under 3% limit

    cb.record_closed_trade(Decimal("-1000"), balance, now=_DAY_1)
    assert cb.is_halted() is True  # cumulative 3% loss trips it


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
    assert cb.is_halted() is True

    cb.record_closed_trade(Decimal("100"), balance, now=_DAY_2)
    assert cb.is_halted() is False
    assert cb.daily_realized_pnl == Decimal("100")


def test_admin_reset_lifts_halt_without_day_rollover() -> None:
    cb = CircuitBreaker(daily_loss_limit_pct=Decimal("0.03"))
    balance = Decimal("100000")

    cb.record_closed_trade(Decimal("-5000"), balance, now=_DAY_1)
    assert cb.is_halted() is True

    cb.reset_daily_halt()
    assert cb.is_halted() is False


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
