"""
RiskManager — hard gate that every trade must pass before reaching the broker.

validate() is the sole entry point. It runs five sequential gates:
  0. Circuit breaker: daily loss halt / per-strategy kill switch, if configured
     (DAILY_CIRCUIT_BREAKER_TRIPPED / STRATEGY_KILL_SWITCH_ACTIVE)
  1. Stop-loss present and valid (MISSING_STOP_LOSS / INVALID_STOP_LOSS)
  2. Position size within 1% rule (RISK_LIMIT_EXCEEDED)
  3. Reward-to-risk ratio meets minimum (INSUFFICIENT_REWARD)
  4. Aggregate portfolio exposure within configured limit (PORTFOLIO_RISK_LIMIT_EXCEEDED)

All rejections are logged to risk.log before raising RiskRejectionError.
Approved trades return a ValidationResult that includes the confirmed
stop_loss_price, calculated take_profit_price, and reward_to_risk_ratio.

This module performs no I/O and no DB calls. The caller (OrderManager, Layer 7)
is responsible for supplying the current account_balance and aggregate risk.
RiskManager itself remains stateless; the optional CircuitBreaker passed in
carries the only mutable state involved in validate().
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel

from app.core.risk.calculator import RiskCalculator, RiskValidationError
from app.core.risk.circuit_breaker import CircuitBreaker
from app.monitoring.logger import risk_logger

MAX_RISK_PCT = Decimal("0.01")  # 1% per-trade rule — never configurable
MAX_PORTFOLIO_RISK_HARD_LIMIT = Decimal("0.10")  # 10% portfolio ceiling — never overridable


# ---------------------------------------------------------------------------
# Request / result models
# ---------------------------------------------------------------------------


class TradeRequest(BaseModel):
    """
    All parameters needed for risk validation of a proposed trade.

    Constructed by OrderManager (Layer 7) before calling validate().
    The account_balance field is a real-time snapshot from the broker.
    """

    trade_id: UUID
    symbol: str
    direction: Literal["BUY", "SELL"]
    quantity: Decimal
    entry_price: Decimal
    stop_loss_price: Decimal | None  # None triggers immediate rejection
    strategy_id: UUID | None = None
    account_balance: Decimal
    take_profit_price: Decimal | None = None  # strategy suggestion; optional
    submit_stop_to_broker: bool = False  # opt-in per-strategy flag


class ValidationResult(BaseModel):
    """
    Returned by RiskManager.validate() when a trade is approved.

    All fields are forwarded to OrderManager for audit logging and
    trade persistence. take_profit_price is always set (either from
    the strategy suggestion or calculated mechanically from R:R).
    """

    trade_id: UUID
    risk_amount: Decimal
    max_quantity: int
    account_balance_at_entry: Decimal  # snapshot at time of validation
    take_profit_price: Decimal
    reward_to_risk_ratio: Decimal


# ---------------------------------------------------------------------------
# Rejection exception
# ---------------------------------------------------------------------------


class RiskRejectionError(Exception):
    """
    Raised by RiskManager.validate() when a trade fails risk checks.

    Carries enough context for the caller to log the rejection and
    surface it to the operator.
    """

    def __init__(self, reason: str, request: TradeRequest) -> None:
        super().__init__(reason)
        self.reason = reason
        self.request = request


# ---------------------------------------------------------------------------
# RiskManager
# ---------------------------------------------------------------------------


class RiskManager:
    """
    Validates a proposed trade against all risk rules.

    Stateless per call — instantiate once and reuse. The optional
    circuit_breaker is the one exception: it is itself stateful, but
    RiskManager only ever reads from it (see Gate 0), never mutates it.

    Args:
        min_reward_to_risk:    Minimum R:R ratio required. Default 2.0 (2:1).
        max_portfolio_risk_pct: Maximum aggregate open exposure as a fraction
                               of account balance. Default 5%. Hard ceiling 10%.
        circuit_breaker:       Optional shared CircuitBreaker instance. When
                               provided, Gate 0 rejects trades while a daily
                               halt or per-strategy kill switch is active.
                               When None (default), this gate is skipped —
                               existing callers that don't wire one up are
                               unaffected.
    """

    def __init__(
        self,
        min_reward_to_risk: Decimal = Decimal("2.0"),
        max_portfolio_risk_pct: Decimal = Decimal("0.05"),
        circuit_breaker: CircuitBreaker | None = None,
    ) -> None:
        if max_portfolio_risk_pct > MAX_PORTFOLIO_RISK_HARD_LIMIT:
            raise ValueError(
                f"max_portfolio_risk_pct {max_portfolio_risk_pct} exceeds "
                f"hard limit {MAX_PORTFOLIO_RISK_HARD_LIMIT}"
            )
        self._calculator = RiskCalculator()
        self._min_reward_to_risk = min_reward_to_risk
        self._max_portfolio_risk_pct = max_portfolio_risk_pct
        self._circuit_breaker = circuit_breaker

    def validate(
        self,
        request: TradeRequest,
        current_aggregate_risk: Decimal = Decimal("0"),
    ) -> ValidationResult:
        """
        Validate a trade request against all risk rules.

        Args:
            request:                The proposed trade with all financial parameters
                                    and the current account balance.
            current_aggregate_risk: Sum of risk_amount across all currently active
                                    (non-terminal) trades. Used for Gate 4 portfolio
                                    check. Defaults to 0 (no existing exposure).
                                    OrderManager computes this from PENDING,
                                    SUBMITTED, and OPEN trades — not just OPEN —
                                    so an in-flight order's risk is reserved the
                                    moment it's created, not only once filled.

        Returns:
            ValidationResult with computed risk_amount, max_quantity,
            account_balance_at_entry snapshot, take_profit_price, and
            reward_to_risk_ratio.

        Raises:
            RiskRejectionError: If the trade fails any gate. All rejections are
                logged to risk.log before raising.
        """
        # --- Gate 0: circuit breaker (daily loss halt / per-strategy kill switch) ---
        if self._circuit_breaker is not None:
            self._validate_circuit_breaker(request)

        # --- Gate 0b: short selling (direction == SELL) ---
        # Checked first and explicitly, rather than letting a SELL request
        # fall through to Gate 2's long-only stop validation, which would
        # otherwise raise a confusing "INVALID_STOP_LOSS" for what is
        # actually a correctly-placed short stop (above entry).
        if request.direction == "SELL":
            self._validate_short_request(request)

        # --- Gate 1: stop-loss is required ---
        if request.stop_loss_price is None:
            self._reject(
                request,
                reason="MISSING_STOP_LOSS",
                detail="No stop_loss_price provided — all trades require a stop-loss",
            )

        # --- Gate 2: stop-loss must be below entry; compute 1% position ceiling ---
        try:
            max_qty = self._calculator.max_quantity(
                account_balance=request.account_balance,
                entry_price=request.entry_price,
                stop_loss_price=request.stop_loss_price,  # type: ignore[arg-type]
            )
        except RiskValidationError as exc:
            self._reject(request, reason="INVALID_STOP_LOSS", detail=str(exc))

        # --- Gate 2 (cont.): actual risk amount must not exceed 1% of balance ---
        risk_amount = self._calculator.risk_amount(
            quantity=request.quantity,
            entry_price=request.entry_price,
            stop_loss_price=request.stop_loss_price,  # type: ignore[arg-type]
        )
        max_allowed = request.account_balance * MAX_RISK_PCT
        if risk_amount > max_allowed:
            self._reject(
                request,
                reason="RISK_LIMIT_EXCEEDED",
                detail=(
                    f"risk_amount {risk_amount:.2f} exceeds 1% limit "
                    f"({max_allowed:.2f}) for balance {request.account_balance:.2f}"
                ),
            )

        # --- Gate 3: reward-to-risk ratio must meet minimum ---
        stop_distance = request.entry_price - request.stop_loss_price  # type: ignore[operator]
        required_take_profit = request.entry_price + (stop_distance * self._min_reward_to_risk)

        if request.take_profit_price is not None:
            if request.take_profit_price < required_take_profit:
                self._reject(
                    request,
                    reason="INSUFFICIENT_REWARD",
                    detail=(
                        f"suggested take_profit {request.take_profit_price} is below "
                        f"required {required_take_profit} "
                        f"(entry={request.entry_price}, stop={request.stop_loss_price}, "
                        f"min_rr={self._min_reward_to_risk})"
                    ),
                )
            take_profit_price = request.take_profit_price
        else:
            # No suggestion — calculate mechanically from minimum R:R
            take_profit_price = required_take_profit

        reward_to_risk = (take_profit_price - request.entry_price) / stop_distance

        # --- Gate 4: aggregate portfolio exposure must not exceed configured max ---
        max_portfolio_risk_amount = request.account_balance * self._max_portfolio_risk_pct
        if current_aggregate_risk + risk_amount > max_portfolio_risk_amount:
            self._reject(
                request,
                reason="PORTFOLIO_RISK_LIMIT_EXCEEDED",
                detail=(
                    f"current_aggregate {current_aggregate_risk:.2f} + "
                    f"new_risk {risk_amount:.2f} = "
                    f"{current_aggregate_risk + risk_amount:.2f} exceeds "
                    f"portfolio limit {max_portfolio_risk_amount:.2f} "
                    f"({self._max_portfolio_risk_pct:.0%} of {request.account_balance:.2f})"
                ),
            )

        return ValidationResult(
            trade_id=request.trade_id,
            risk_amount=risk_amount,
            max_quantity=max_qty,  # type: ignore[possibly-undefined]
            account_balance_at_entry=request.account_balance,
            take_profit_price=take_profit_price,
            reward_to_risk_ratio=reward_to_risk,
        )

    # ------------------------------------------------------------------
    # Circuit breaker — daily loss halt / per-strategy kill switch
    # ------------------------------------------------------------------

    def _validate_circuit_breaker(self, request: TradeRequest) -> None:
        """
        Gate 0: reject if the shared CircuitBreaker has tripped the daily
        halt, or has killed this specific strategy. Read-only — RiskManager
        never mutates the breaker; only OrderManager.close_position() does,
        via CircuitBreaker.record_closed_trade().
        """
        cb = self._circuit_breaker
        assert cb is not None  # caller already checked

        if cb.is_halted():
            self._reject(
                request,
                reason="DAILY_CIRCUIT_BREAKER_TRIPPED",
                detail=(
                    f"Daily realized loss {cb.daily_realized_pnl:.2f} has reached "
                    "the configured halt threshold for today — no new trades "
                    "until the next trading day, or an admin reset."
                ),
            )

        if cb.is_strategy_killed(request.strategy_id):
            self._reject(
                request,
                reason="STRATEGY_KILL_SWITCH_ACTIVE",
                detail=(
                    f"Strategy {request.strategy_id} has been disabled by the "
                    "circuit breaker (repeated consecutive losses, or an admin "
                    "kill) — re-enable it explicitly to resume trading."
                ),
            )

    # ------------------------------------------------------------------
    # Short selling — not implemented (see ENABLE_SHORT_SELLING in config.py)
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_short_request(request: TradeRequest) -> None:
        """
        Gate for SELL (short) trades. Short selling is not implemented.

        When ENABLE_SHORT_SELLING=false (default), short requests are
        cleanly rejected here with a specific reason rather than falling
        through to the long-only stop validation in Gate 2.

        When ENABLE_SHORT_SELLING=true, this raises NotImplementedError —
        flipping the flag does not yet enable real shorting; it only moves
        the failure here so the gap stays visible instead of the system
        silently mis-pricing risk. Implementing it for real requires, at
        minimum:
          - RiskCalculator.max_quantity_short / risk_amount_short (stop
            above entry; stubbed but not implemented — see calculator.py)
          - RiskManager: short-side R:R math (take_profit BELOW entry)
          - OrderManager: inverted PnL on close
            (pnl = (entry_price − fill_price) × qty, already the close-leg
            formula in close_position — but the *entry* leg's accounting
            and audit fields throughout submit_order assume a long)
          - PositionMonitor: inverted hit-check (price >= stop closes a
            short, not price <= stop; price <= take_profit, not >=)
          - Broker layer: margin / short-availability checks before
            submitting a SELL-to-open order
        """
        from app.config import get_settings

        if not get_settings().enable_short_selling:
            RiskManager._reject(
                request,
                reason="SHORT_SELLING_DISABLED",
                detail=(
                    "RiskManager is long-only by design. Set ENABLE_SHORT_SELLING=true "
                    "to opt into the (not yet implemented) short-selling code path — "
                    "see RiskManager._validate_short_request for what's missing."
                ),
            )
            return  # unreachable: _reject always raises; satisfies type checkers

        raise NotImplementedError(
            "Short selling is not implemented even though ENABLE_SHORT_SELLING=true. "
            "See RiskManager._validate_short_request docstring for required work."
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _reject(request: TradeRequest, reason: str, detail: str) -> None:
        """Log the rejection to risk.log and raise RiskRejectionError."""
        risk_logger.warning(
            "Trade rejected by RiskManager",
            extra={
                "trade_id": str(request.trade_id),
                "reason": reason,
                "detail": detail,
                "symbol": request.symbol,
                "direction": request.direction,
                "quantity": str(request.quantity),
                "entry_price": str(request.entry_price),
                "stop_loss_price": str(request.stop_loss_price),
                "take_profit_price": str(request.take_profit_price),
                "account_balance": str(request.account_balance),
                "strategy_id": str(request.strategy_id) if request.strategy_id else None,
            },
        )
        raise RiskRejectionError(reason=f"{reason}: {detail}", request=request)
