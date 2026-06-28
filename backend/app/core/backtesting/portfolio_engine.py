"""
PortfolioBacktestEngine — runs multiple strategy/symbol pairs bar-by-bar in
lock-step with a single shared account balance and aggregate risk cap.

The 10% portfolio risk hard limit is enforced across all simultaneously open
simulated positions, exactly as in live trading. When a new trade for slot N
would push combined exposure over the limit, it is rejected — even if that
slot would pass the single-trade 1% gate in isolation.

Usage:
    slots = [
        PortfolioSlot(strategy=ma,  symbol="AAPL", bars=aapl_bars, strategy_type="moving_average"),
        PortfolioSlot(strategy=mr,  symbol="MSFT", bars=msft_bars, strategy_type="mean_reversion"),
    ]
    engine = PortfolioBacktestEngine(RiskManager())
    result = await engine.run(slots, account_balance=Decimal("100000"))

Lock-step semantics:
    On bar index i, ALL slots process bar i before any slot advances to i+1.
    Phase 1 (exits) completes for every slot before Phase 2 (entries) begins,
    so aggregate risk reflects current open positions accurately when new trades
    are sized and validated.

Slots with fewer bars than the longest slot are silently skipped once their
data is exhausted. Any trade still open at end-of-data is closed at the last
bar's close price.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from uuid import uuid4

from app.brokers.base import PriceBar
from app.core.backtesting.engine import (
    BacktestMetrics,
    BacktestResult,
    SimulatedTrade,
    _compute_metrics,
    apply_slippage,
)
from app.core.risk.manager import RiskManager, RiskRejectionError, TradeRequest
from app.core.strategy_engine.base import BaseStrategy, MarketData, RiskParams

# ---------------------------------------------------------------------------
# Input / output types
# ---------------------------------------------------------------------------


@dataclass
class PortfolioSlot:
    """One strategy/symbol pair to include in the portfolio backtest."""

    strategy: BaseStrategy
    symbol: str
    bars: list[PriceBar]
    strategy_type: str = "unknown"
    strategy_config: dict = field(default_factory=dict)


@dataclass
class PortfolioBacktestResult:
    """Full result from a portfolio backtest run."""

    account_balance: Decimal
    start_time: datetime
    end_time: datetime

    per_strategy: list[BacktestResult]
    """One BacktestResult per input slot, in slot order."""

    combined_equity_curve: list[tuple[datetime, Decimal]]
    """Chronological (timestamp, cumulative_pnl) events — one entry per trade close."""

    combined_metrics: BacktestMetrics
    """Aggregate metrics computed across all trades from all slots."""


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class PortfolioBacktestEngine:
    """
    Drives multiple strategy/symbol pairs through historical bars simultaneously.

    Args:
        risk_manager:         Shared RiskManager instance. The same aggregate
                              risk cap that governs live trading applies here.
        slippage_pct:         Unfavorable price adjustment applied to every fill,
                              same semantics as BacktestingEngine. Default "0.0005".
        commission_per_trade: Flat fee on entry and exit legs, same semantics
                              as BacktestingEngine. Default "1.00".
    """

    def __init__(
        self,
        risk_manager: RiskManager,
        slippage_pct: Decimal = Decimal("0.0005"),
        commission_per_trade: Decimal = Decimal("1.00"),
    ) -> None:
        self._risk = risk_manager
        self._slippage_pct = slippage_pct
        self._commission_per_trade = commission_per_trade

    async def run(
        self,
        slots: list[PortfolioSlot],
        account_balance: Decimal,
    ) -> PortfolioBacktestResult:
        if not slots:
            raise ValueError("Need at least one slot to run a portfolio backtest.")
        if any(len(s.bars) < 2 for s in slots):
            raise ValueError("Every slot must have at least 2 bars.")

        n = len(slots)
        max_bars = max(len(s.bars) for s in slots)

        # Per-slot mutable state
        open_trades: list[SimulatedTrade | None] = [None] * n
        completed: list[list[SimulatedTrade]] = [[] for _ in range(n)]
        sig_generated: list[int] = [0] * n
        sig_rejected: list[int] = [0] * n

        # Combined equity tracking
        equity_events: list[tuple[datetime, Decimal]] = []
        cumulative_pnl = Decimal("0")

        for bar_idx in range(1, max_bars):

            # ------------------------------------------------------------------
            # Phase 1 — check exits for all slots that have an open trade
            # ------------------------------------------------------------------
            just_exited: set[int] = set()
            for si, slot in enumerate(slots):
                if bar_idx >= len(slot.bars) or open_trades[si] is None:
                    continue

                bar = slot.bars[bar_idx]
                trade = _apply_exit(
                    open_trades[si], bar, self._slippage_pct, self._commission_per_trade  # type: ignore[arg-type]
                )

                if trade.exit_price is not None:
                    completed[si].append(trade)
                    open_trades[si] = None
                    just_exited.add(si)
                    cumulative_pnl += trade.pnl  # type: ignore[operator]
                    equity_events.append((bar.timestamp, cumulative_pnl))

            # ------------------------------------------------------------------
            # Phase 2 — generate signals; aggregate risk updated slot-by-slot
            # ------------------------------------------------------------------
            agg_risk = sum(
                t.risk_amount for t in open_trades if t is not None
            )

            for si, slot in enumerate(slots):
                # Skip: slot exhausted, already in a trade, or just exited this bar
                if bar_idx >= len(slot.bars) or open_trades[si] is not None or si in just_exited:
                    continue

                bar = slot.bars[bar_idx]
                history = slot.bars[: bar_idx + 1]

                market_data = MarketData(
                    symbol=slot.symbol,
                    current_price=bar.close,
                    bars=history,
                    timestamp=bar.timestamp,
                )
                signal = await slot.strategy.generate_signal(market_data)

                if signal.action in ("HOLD", "SELL"):
                    continue

                sig_generated[si] += 1

                # Fill at next bar's open (no look-ahead bias)
                if bar_idx + 1 >= len(slot.bars):
                    continue
                fill_bar = slot.bars[bar_idx + 1]
                raw_fill_price = fill_bar.open

                # Risk gates run on the intended (pre-slippage) price, matching
                # live OrderManager — see BacktestingEngine.run() for the same
                # reasoning. Only the recorded fill/PnL reflects slippage.
                stop = (
                    signal.stop_loss_price
                    or (raw_fill_price * Decimal("0.97")).quantize(Decimal("0.01"))
                )
                risk_params = RiskParams(
                    account_balance=account_balance,
                    entry_price=raw_fill_price,
                    stop_loss_price=stop,
                )
                quantity = await slot.strategy.calculate_position_size(risk_params)
                if quantity <= 0:
                    sig_rejected[si] += 1
                    continue

                request = TradeRequest(
                    trade_id=uuid4(),
                    symbol=slot.symbol,
                    direction="BUY",
                    quantity=Decimal(str(quantity)),
                    entry_price=raw_fill_price,
                    stop_loss_price=stop,
                    account_balance=account_balance,
                    take_profit_price=signal.take_profit_price,
                )
                try:
                    validation = self._risk.validate(
                        request, current_aggregate_risk=agg_risk
                    )
                except RiskRejectionError:
                    sig_rejected[si] += 1
                    continue

                # Long entry: slippage works against us — we pay slightly more.
                fill_price = apply_slippage(raw_fill_price, 1, self._slippage_pct)
                risk_amt = Decimal(str(quantity)) * (fill_price - stop)
                open_trades[si] = SimulatedTrade(
                    trade_id=request.trade_id,
                    symbol=slot.symbol,
                    direction="BUY",
                    entry_price=fill_price,
                    stop_loss_price=stop,
                    take_profit_price=validation.take_profit_price,
                    quantity=quantity,
                    risk_amount=risk_amt,
                    entry_bar_index=bar_idx + 1,
                    entry_time=fill_bar.timestamp,
                    commission_paid=self._commission_per_trade,  # entry leg
                )
                # Update aggregate so subsequent slots in this bar see the new exposure
                agg_risk += risk_amt

        # ------------------------------------------------------------------
        # Close any trades still open at end of data
        # ------------------------------------------------------------------
        for si, slot in enumerate(slots):
            if open_trades[si] is None:
                continue
            last_bar = slot.bars[-1]
            trade = open_trades[si]
            assert trade is not None
            # Long exit: slippage works against us — we receive slightly less.
            trade.exit_price = apply_slippage(last_bar.close, -1, self._slippage_pct)
            trade.exit_time = last_bar.timestamp
            trade.exit_reason = "END_OF_DATA"
            trade.commission_paid += self._commission_per_trade  # exit leg
            trade.pnl = (
                (trade.exit_price - trade.entry_price) * trade.quantity - trade.commission_paid
            )
            completed[si].append(trade)
            cumulative_pnl += trade.pnl
            equity_events.append((last_bar.timestamp, cumulative_pnl))

        # Sort equity curve chronologically (slots may close trades out of order)
        equity_events.sort(key=lambda e: e[0])

        # ------------------------------------------------------------------
        # Build per-strategy BacktestResult objects
        # ------------------------------------------------------------------
        per_strategy: list[BacktestResult] = []
        all_trades: list[SimulatedTrade] = []

        for si, slot in enumerate(slots):
            trades = completed[si]
            all_trades.extend(trades)
            per_strategy.append(
                BacktestResult(
                    symbol=slot.symbol,
                    strategy_type=slot.strategy_type,
                    strategy_config=slot.strategy_config,
                    account_balance=account_balance,
                    start_time=slot.bars[0].timestamp,
                    end_time=slot.bars[-1].timestamp,
                    trades=trades,
                    metrics=_compute_metrics(
                        trades=trades,
                        initial_balance=account_balance,
                        bars_tested=len(slot.bars),
                        signals_generated=sig_generated[si],
                        signals_rejected=sig_rejected[si],
                    ),
                )
            )

        combined_metrics = _compute_metrics(
            trades=all_trades,
            initial_balance=account_balance,
            bars_tested=sum(len(s.bars) for s in slots),
            signals_generated=sum(sig_generated),
            signals_rejected=sum(sig_rejected),
        )

        return PortfolioBacktestResult(
            account_balance=account_balance,
            start_time=min(s.bars[0].timestamp for s in slots),
            end_time=max(s.bars[-1].timestamp for s in slots),
            per_strategy=per_strategy,
            combined_equity_curve=equity_events,
            combined_metrics=combined_metrics,
        )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _apply_exit(
    trade: SimulatedTrade,
    bar: PriceBar,
    slippage_pct: Decimal,
    commission_per_trade: Decimal,
) -> SimulatedTrade:
    """
    Check bar for stop-loss or take-profit hit.

    Conservative tie-break: if both levels are breached on the same bar,
    the stop wins. This matches BacktestingEngine._check_exit behaviour,
    including identical slippage/commission friction on the exit fill.
    """
    if bar.low <= trade.stop_loss_price:
        trade.exit_price = apply_slippage(trade.stop_loss_price, -1, slippage_pct)
        trade.exit_reason = "STOP_LOSS"
    elif bar.high >= trade.take_profit_price:
        trade.exit_price = apply_slippage(trade.take_profit_price, -1, slippage_pct)
        trade.exit_reason = "TAKE_PROFIT"

    if trade.exit_price is not None:
        trade.exit_time = bar.timestamp
        trade.commission_paid += commission_per_trade  # exit leg
        trade.pnl = (
            (trade.exit_price - trade.entry_price) * trade.quantity - trade.commission_paid
        )

    return trade
