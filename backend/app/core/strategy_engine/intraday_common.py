"""Shared pieces of the day-trade strategies (vwap_reclaim, opening_range_breakout)."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from decimal import Decimal

from app.core import market_calendar
from app.core.risk.calculator import RiskCalculator
from app.core.strategy_engine.base import BaseStrategy, MarketData, RiskParams, Signal
from app.core.strategy_engine.filters import regime_ok
from app.core.strategy_engine.indicators import sma


class DayTradeStrategy(BaseStrategy):
    """Base for strategies that enter and leave the same session (needs 5-minute bars)."""

    needs_intraday = True

    def __init__(self, config: dict) -> None:
        self.volume_mult = Decimal(str(config.get("volume_mult", "1.2")))
        self.max_chase_pct = Decimal(str(config.get("max_chase_pct", "0.003")))
        self.rr_ratio = Decimal(str(config.get("rr_ratio", "2.0")))
        self.trend_period = int(config.get("trend_period", 50))
        self.regime_symbol = str(config.get("regime_symbol", "SPY") or "").upper()
        self.regime_period = int(config.get("regime_period", 200))
        self._calculator = RiskCalculator()
        if self.volume_mult < 0 or self.max_chase_pct < 0:
            raise ValueError("volume_mult and max_chase_pct must be >= 0")
        if self.rr_ratio <= 0:
            raise ValueError("rr_ratio must be > 0")
        if self.trend_period < 1 or self.regime_period < 1:
            raise ValueError("trend_period and regime_period must be >= 1")

    def daily_context_ok(self, market_data: MarketData) -> bool:
        """Daily close above its trend average, and the market barometer is up."""
        closes = [b.close for b in market_data.bars]
        if len(closes) < self.trend_period or closes[-1] <= sma(closes, self.trend_period):
            return False
        if not self.regime_symbol:
            return True
        regime_bars = (
            market_data.bars if market_data.symbol.upper() == self.regime_symbol
            else market_data.extra_bars.get(self.regime_symbol, [])
        )
        return regime_ok(regime_bars, self.regime_period)

    def volume_ok(self, bars) -> bool:
        """The last bar's volume is at least volume_mult times the average of the earlier bars."""
        earlier = bars[:-1]
        avg = Decimal(sum(b.volume for b in earlier)) / len(earlier)
        return bars[-1].volume >= self.volume_mult * avg

    def buy(self, market_data: MarketData, stop: Decimal) -> Signal:
        """BUY at the current price with a rr_ratio target and a deadline before the close."""
        price = market_data.current_price
        stop = stop.quantize(Decimal("0.01"))
        return Signal(
            symbol=market_data.symbol, action="BUY", entry_price=price, stop_loss_price=stop,
            take_profit_price=(price + self.rr_ratio * (price - stop)).quantize(Decimal("0.01")),
            timestamp=market_data.timestamp, exit_deadline=exit_deadline(market_data.timestamp),
        )

    def required_extra_symbols(self) -> list[str]:
        return [self.regime_symbol] if self.regime_symbol else []

    async def calculate_position_size(self, risk_params: RiskParams) -> int:
        """Return the maximum safe quantity using the 1% risk rule."""
        try:
            return self._calculator.max_quantity(
                risk_params.account_balance, risk_params.entry_price, risk_params.stop_loss_price
            )
        except Exception:
            return 0

    def _hold(self, symbol: str) -> Signal:
        return Signal(symbol=symbol, action="HOLD", timestamp=datetime.now(UTC))


def exit_deadline(now: datetime) -> datetime | None:
    """Five minutes before today's close (early-close days included); None if not a session."""
    bounds = market_calendar.session_bounds(market_calendar.trading_date(now))
    return bounds[1] - timedelta(minutes=5) if bounds else None


def et_time(now: datetime) -> time:
    return now.astimezone(market_calendar.ET).time()


def parse_et(value: str, name: str) -> time:
    try:
        return time.fromisoformat(str(value))
    except ValueError as exc:
        raise ValueError(f"{name} must be HH:MM, got {value!r}") from exc
