"""
Maps raw ib_async objects to internal Pydantic broker models.

All conversions from IBKR-specific types live here, keeping IBKRClient
free of mapping logic and making the translations easy to test in isolation.
"""

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from uuid import UUID

import datetime as dt

from ib_async import AccountValue, BarData
from ib_async import Position as IBPosition
from ib_async import Trade

from app.brokers.base import AccountSummary, OrderResult, Position, PriceBar


# ---------------------------------------------------------------------------
# AccountSummary
# ---------------------------------------------------------------------------

# IBKR AccountValue tags we care about.  Full list:
# https://ibkrcampus.com/ibkr-api-page/trader-workstation-api/#account-summary-tags
_TAG_NET_LIQUIDATION = "NetLiquidation"
_TAG_CASH_BALANCE = "CashBalance"
_TAG_BUYING_POWER = "BuyingPower"
_TAG_GROSS_POSITION_VALUE = "GrossPositionValue"
_TAG_UNREALIZED_PNL = "UnrealizedPnL"
_TAG_REALIZED_PNL = "RealizedPnL"


def _to_decimal(value: str | None) -> Decimal:
    """Convert a string value to Decimal, returning 0 on failure."""
    if not value:
        return Decimal("0")
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return Decimal("0")


def map_account_summary(account_values: list[AccountValue]) -> AccountSummary:
    """
    Aggregate a list of AccountValue entries into a single AccountSummary.

    IBKR returns one AccountValue per tag per account. We collect the tags
    we need and discard the rest.
    """
    if not account_values:
        raise ValueError("Cannot map empty account_values list to AccountSummary")

    # Build a tag → value lookup (last write wins for duplicate tags)
    tag_map: dict[str, str] = {av.tag: av.value for av in account_values}
    account_id = account_values[0].account

    return AccountSummary(
        account_id=account_id,
        net_liquidation=_to_decimal(tag_map.get(_TAG_NET_LIQUIDATION)),
        cash_balance=_to_decimal(tag_map.get(_TAG_CASH_BALANCE)),
        buying_power=_to_decimal(tag_map.get(_TAG_BUYING_POWER)),
        gross_position_value=_to_decimal(tag_map.get(_TAG_GROSS_POSITION_VALUE)),
        unrealized_pnl=_to_decimal(tag_map.get(_TAG_UNREALIZED_PNL)),
        realized_pnl=_to_decimal(tag_map.get(_TAG_REALIZED_PNL)),
        currency="USD",
    )


# ---------------------------------------------------------------------------
# Position
# ---------------------------------------------------------------------------


def map_position(ib_position: IBPosition) -> Position:
    """
    Map an ib_async Position namedtuple to the internal Position model.

    ib_async Position fields: account, contract, position, avgCost
    Note: market_price and market_value are not always present in position
    data — they are populated separately by the market data feed (Layer 5).
    """
    quantity = _to_decimal(str(ib_position.position))
    avg_cost = _to_decimal(str(ib_position.avgCost))
    market_value = quantity * avg_cost  # best estimate without live price

    return Position(
        account_id=ib_position.account,
        symbol=ib_position.contract.symbol,
        quantity=quantity,
        average_cost=avg_cost,
        market_price=Decimal("0"),  # populated by MarketDataFeed (Layer 5)
        market_value=market_value,
        unrealized_pnl=Decimal("0"),  # populated by MarketDataFeed (Layer 5)
    )


# ---------------------------------------------------------------------------
# OrderResult
# ---------------------------------------------------------------------------

# Maps IBKR order status strings to our internal status literals.
# Full IBKR status reference:
# https://ibkrcampus.com/ibkr-api-page/trader-workstation-api/#order-status
_STATUS_MAP: dict[str, str] = {
    "Filled": "FILLED",
    "PartiallyFilled": "PARTIAL",
    "Cancelled": "REJECTED",
    "ApiCancelled": "REJECTED",
    "Inactive": "REJECTED",
    "Submitted": "PARTIAL",  # acknowledged but not yet filled
    "PreSubmitted": "PARTIAL",
}


def map_price_bar(bar: BarData, bar_size: str) -> PriceBar:
    """
    Map an ib_async BarData to the internal PriceBar model.

    bar.date is a datetime.datetime for intraday bars and a datetime.date for
    daily bars. Both are normalised to a timezone-aware UTC datetime.
    """
    raw_date = bar.date
    if isinstance(raw_date, dt.datetime):
        timestamp = raw_date if raw_date.tzinfo else raw_date.replace(tzinfo=dt.timezone.utc)
    elif isinstance(raw_date, dt.date):
        timestamp = dt.datetime(raw_date.year, raw_date.month, raw_date.day, tzinfo=dt.timezone.utc)
    else:
        # Fallback: string in "YYYYMMDD" or "YYYYMMDD HH:MM:SS" format
        raw_str = str(raw_date).strip()
        if " " in raw_str:
            timestamp = dt.datetime.strptime(raw_str, "%Y%m%d %H:%M:%S").replace(
                tzinfo=dt.timezone.utc
            )
        else:
            parsed = dt.datetime.strptime(raw_str[:8], "%Y%m%d")
            timestamp = parsed.replace(tzinfo=dt.timezone.utc)

    return PriceBar(
        timestamp=timestamp,
        open=_to_decimal(str(bar.open)),
        high=_to_decimal(str(bar.high)),
        low=_to_decimal(str(bar.low)),
        close=_to_decimal(str(bar.close)),
        volume=max(0, int(bar.volume)),  # IBKR returns -1 if unavailable
        bar_size=bar_size,
    )


def map_order_result(trade_id: UUID, trade: Trade) -> OrderResult:
    """
    Map an ib_async Trade object to the internal OrderResult model.

    Called after the order is done (filled, cancelled, or errored).
    """
    order_status = trade.orderStatus.status
    status = _STATUS_MAP.get(order_status, "ERROR")

    fill = trade.orderStatus
    avg_price = _to_decimal(str(fill.avgFillPrice) if fill.avgFillPrice else None)
    filled_qty = _to_decimal(str(fill.filled) if fill.filled else None)

    # Collect any error messages from the trade log
    error_message: str | None = None
    if trade.log:
        errors = [
            entry.message
            for entry in trade.log
            if "error" in entry.message.lower() or "warning" in entry.message.lower()
        ]
        if errors:
            error_message = "; ".join(errors)

    return OrderResult(
        trade_id=trade_id,
        broker_order_id=str(trade.order.orderId),
        status=status,  # type: ignore[arg-type]
        filled_quantity=filled_qty,
        avg_fill_price=avg_price,
        error_message=error_message,
        timestamp=datetime.now(timezone.utc),
    )
