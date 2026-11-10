"""Result dataclasses for a single-strategy backtest."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from uuid import UUID

# ---------------------------------------------------------------------------
# Domain models
# ---------------------------------------------------------------------------


@dataclass
class SimulatedTrade:
    trade_id: UUID
    symbol: str
    direction: str          # "BUY" (shorts skipped for now)
    entry_price: Decimal
    stop_loss_price: Decimal
    take_profit_price: Decimal
    quantity: int
    risk_amount: Decimal
    entry_bar_index: int
    entry_time: datetime
    exit_price: Decimal | None = None
    exit_time: datetime | None = None
    exit_reason: str | None = None   # "STOP_LOSS" | "TAKE_PROFIT" | "END_OF_DATA"
    pnl: Decimal | None = None
    commission_paid: Decimal = Decimal("0")  # entry + exit leg, already netted into pnl


@dataclass
class BacktestMetrics:
    """Computed summary metrics for a completed backtest (15.2)."""

    trade_count: int
    win_count: int
    loss_count: int
    win_rate_pct: float          # 0–100

    total_return: Decimal        # sum of all P&L
    total_return_pct: float      # total_return / initial_balance × 100
    avg_trade_pnl: Decimal
    avg_winner: Decimal
    avg_loser: Decimal
    largest_winner: Decimal
    largest_loser: Decimal

    max_drawdown_pct: float      # peak-to-trough on cumulative equity, 0–100
    sharpe_ratio: float          # annualised Sharpe (daily returns, rf=0)

    bars_tested: int
    signals_generated: int
    signals_rejected: int        # by RiskManager
    total_commission_paid: Decimal = Decimal("0")
    ending_balance: Decimal = Decimal("0")  # initial balance + total_return


@dataclass
class BacktestResult:
    symbol: str
    strategy_type: str
    strategy_config: dict
    account_balance: Decimal
    start_time: datetime
    end_time: datetime
    trades: list[SimulatedTrade] = field(default_factory=list)
    metrics: BacktestMetrics | None = None
