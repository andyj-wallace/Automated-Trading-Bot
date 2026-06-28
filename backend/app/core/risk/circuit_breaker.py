"""
CircuitBreaker — daily-loss trading halt and per-strategy kill switch.

Unlike RiskManager (stateless, instantiate-once-reuse, no I/O), this
component is explicitly stateful: it accumulates realized PnL and
consecutive-loss counts over time, so exactly one instance must be shared
across the whole process (RiskManager, OrderManager, and any admin API that
inspects/resets it) — see app/main.py lifespan and app/dependencies.py.

Two independent gates:
  1. Daily circuit breaker — halts ALL new trades for the rest of the
     trading day once cumulative realized loss reaches `daily_loss_limit_pct`
     of the day's starting balance. Auto-resets at the first recorded trade
     of a new calendar day (UTC); can also be lifted early by an admin via
     reset_daily_halt().
  2. Per-strategy kill switch — disables a single strategy_id after
     `max_consecutive_losses` losing trades in a row. A winning trade resets
     that strategy's streak. Once killed, a strategy stays disabled until
     explicitly re-enabled via reset_strategy() — it does not self-heal.

Both gates are read by RiskManager.validate() (Gate 0) and updated by
OrderManager.close_position() via record_closed_trade(). Neither gate places
or cancels orders itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

DEFAULT_DAILY_LOSS_LIMIT_PCT = Decimal("0.03")  # between the 1% per-trade rule and 10% hard cap
DEFAULT_MAX_CONSECUTIVE_LOSSES = 5


@dataclass
class _StrategyState:
    consecutive_losses: int = 0
    killed: bool = False


class CircuitBreaker:
    """
    Args:
        daily_loss_limit_pct:   Fraction of the day's starting balance that,
            once lost (realized, cumulative), halts all new trades until the
            next calendar day or an admin reset. Default 3%.
        max_consecutive_losses: Consecutive losing trades for a single
            strategy_id before that strategy is auto-disabled. Default 5.
            Trades with no strategy_id (None) never trigger an auto-kill —
            there is nothing to disable.
    """

    def __init__(
        self,
        daily_loss_limit_pct: Decimal = DEFAULT_DAILY_LOSS_LIMIT_PCT,
        max_consecutive_losses: int = DEFAULT_MAX_CONSECUTIVE_LOSSES,
    ) -> None:
        self._daily_loss_limit_pct = daily_loss_limit_pct
        self._max_consecutive_losses = max_consecutive_losses

        self._current_day: date | None = None
        self._day_starting_balance: Decimal = Decimal("0")
        self._daily_realized_pnl: Decimal = Decimal("0")
        self._halted_today: bool = False

        self._strategies: dict[UUID, _StrategyState] = {}

    # ------------------------------------------------------------------
    # Recording — called by OrderManager after every trade close
    # ------------------------------------------------------------------

    def record_closed_trade(
        self,
        pnl: Decimal,
        account_balance: Decimal,
        strategy_id: UUID | None = None,
        *,
        now: datetime | None = None,
    ) -> None:
        """
        Update daily PnL and the strategy's consecutive-loss streak.

        `account_balance` is used as the day's starting balance the first
        time a trade is recorded on a new calendar day, sizing the loss
        threshold for that day.
        """
        self._roll_day_if_needed(account_balance, now=now)
        self._daily_realized_pnl += pnl

        threshold = self._day_starting_balance * self._daily_loss_limit_pct
        if self._daily_realized_pnl <= -threshold:
            self._halted_today = True

        if strategy_id is not None:
            state = self._strategies.setdefault(strategy_id, _StrategyState())
            if pnl < 0:
                state.consecutive_losses += 1
                if state.consecutive_losses >= self._max_consecutive_losses:
                    state.killed = True
            else:
                state.consecutive_losses = 0

    def _roll_day_if_needed(self, account_balance: Decimal, *, now: datetime | None) -> None:
        today = (now or datetime.now(UTC)).date()
        if self._current_day != today:
            self._current_day = today
            self._day_starting_balance = account_balance
            self._daily_realized_pnl = Decimal("0")
            self._halted_today = False

    # ------------------------------------------------------------------
    # Daily halt — queries + admin override
    # ------------------------------------------------------------------

    def is_halted(self) -> bool:
        """True if today's cumulative realized loss has tripped the halt."""
        return self._halted_today

    @property
    def daily_realized_pnl(self) -> Decimal:
        return self._daily_realized_pnl

    @property
    def day_starting_balance(self) -> Decimal:
        return self._day_starting_balance

    def reset_daily_halt(self) -> None:
        """Admin override: lift today's halt without waiting for day rollover."""
        self._halted_today = False

    # ------------------------------------------------------------------
    # Per-strategy kill switch — queries + admin control
    # ------------------------------------------------------------------

    def is_strategy_killed(self, strategy_id: UUID | None) -> bool:
        if strategy_id is None:
            return False
        state = self._strategies.get(strategy_id)
        return state.killed if state else False

    def consecutive_losses(self, strategy_id: UUID) -> int:
        state = self._strategies.get(strategy_id)
        return state.consecutive_losses if state else 0

    def killed_strategy_ids(self) -> list[UUID]:
        return [sid for sid, state in self._strategies.items() if state.killed]

    def kill_strategy(self, strategy_id: UUID) -> None:
        """Manually disable a strategy (admin kill switch)."""
        self._strategies.setdefault(strategy_id, _StrategyState()).killed = True

    def reset_strategy(self, strategy_id: UUID) -> None:
        """Re-enable a strategy and clear its loss streak."""
        self._strategies[strategy_id] = _StrategyState()
