"""Pure Decimal indicator helpers shared by strategies, filters and the scheduler."""

from decimal import Decimal

from app.brokers.base import PriceBar


def sma(values: list[Decimal], period: int) -> Decimal:
    """Simple moving average of the last `period` values (caller guarantees len >= period)."""
    return sum(values[-period:], Decimal("0")) / Decimal(period)


def atr(bars: list[PriceBar], period: int) -> Decimal:
    """Average true range of the last `period` bars; needs at least period + 1 bars."""
    trs = [
        max(cur.high - cur.low, abs(cur.high - prev.close), abs(cur.low - prev.close))
        for prev, cur in zip(bars[-period - 1:-1], bars[-period:], strict=True)
    ]
    return sum(trs, Decimal("0")) / Decimal(len(trs))


def avg_dollar_volume(bars: list[PriceBar], period: int = 20) -> Decimal:
    """Average close × volume of the last `period` bars; 0 with no bars."""
    recent = bars[-period:]
    if not recent:
        return Decimal("0")
    return sum((b.close * b.volume for b in recent), Decimal("0")) / Decimal(len(recent))


def session_vwap_series(bars: list[PriceBar]) -> list[Decimal]:
    """Cumulative VWAP after each bar; zero-volume bars repeat the previous value (0 before any volume)."""
    out: list[Decimal] = []
    value_sum = Decimal("0")
    volume_sum = 0
    for b in bars:
        if b.volume > 0:
            typical = b.wap if b.wap is not None else (b.high + b.low + b.close) / 3
            value_sum += typical * b.volume
            volume_sum += b.volume
        out.append(value_sum / volume_sum if volume_sum else Decimal("0"))
    return out
