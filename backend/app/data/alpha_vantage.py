"""Alpha Vantage DIVIDENDS client. Never raises; failures come back as FetchResult.error."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Protocol

import httpx

from app.core import market_calendar

# httpx logs full request URLs (which carry the API key) at INFO.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

MayRequest = Callable[[], Awaitable[bool]]
URL = "https://www.alphavantage.co/query"
MAX_ATTEMPTS = 2
MAX_AMOUNT = Decimal("1e10")  # dividend_events.amount is Numeric(18, 8): at most 10 whole digits


@dataclass(frozen=True)
class DividendRow:
    ex_dividend_date: date
    declaration_date: date | None
    record_date: date | None
    payment_date: date | None
    amount: Decimal
    declared: bool  # ex_dividend_date > today (ET): declared, not yet paid


@dataclass(frozen=True)
class FetchResult:
    ok: bool
    rows: list[DividendRow] = field(default_factory=list)
    dropped: int = 0
    error: str | None = None


class DividendSource(Protocol):
    async def fetch(self, symbol: str, may_request: MayRequest) -> FetchResult: ...


def _date(value: object) -> date | None:
    if not isinstance(value, str) or value in ("", "None"):
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def _amount(value: object) -> Decimal | None:
    if not isinstance(value, str):
        return None
    try:
        amount = Decimal(value)
    except InvalidOperation:
        return None
    return amount if amount.is_finite() and 0 < amount < MAX_AMOUNT else None


def parse_dividends(payload: object, today: date) -> FetchResult:
    """Turn an Alpha Vantage DIVIDENDS payload into rows, dropping malformed and repeated ones."""
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        return FetchResult(ok=False, error="no_data_list")
    rows: list[DividendRow] = []
    seen: set[date] = set()
    dropped = 0
    for raw in payload["data"]:
        if not isinstance(raw, dict):
            dropped += 1
            continue
        ex = _date(raw.get("ex_dividend_date"))
        amount = _amount(raw.get("amount"))
        # One row per ex-date (the first, i.e. newest, wins): a repeat would fail the upsert.
        if ex is None or amount is None or ex in seen:
            dropped += 1
            continue
        seen.add(ex)
        rows.append(
            DividendRow(
                ex_dividend_date=ex,
                declaration_date=_date(raw.get("declaration_date")),
                record_date=_date(raw.get("record_date")),
                payment_date=_date(raw.get("payment_date")),
                amount=amount,
                declared=ex > today,
            )
        )
    return FetchResult(ok=True, rows=rows, dropped=dropped)


class AlphaVantageClient:
    def __init__(
        self,
        api_key: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 10.0,
        clock: Callable[[], datetime] = market_calendar.now_et,
    ) -> None:
        self._api_key = api_key
        self._transport = transport
        self._timeout = timeout
        self._clock = clock

    async def fetch(self, symbol: str, may_request: MayRequest) -> FetchResult:
        """Fetch dividends for `symbol`, retrying a transport error once within budget."""
        try:
            return await self._fetch(symbol, may_request)
        except Exception as exc:
            return FetchResult(ok=False, error=f"unexpected:{type(exc).__name__}")

    async def _fetch(self, symbol: str, may_request: MayRequest) -> FetchResult:
        exc: httpx.TransportError | None = None
        for _ in range(MAX_ATTEMPTS):
            if not await may_request():
                return FetchResult(ok=False, error="budget_exhausted")
            try:
                async with httpx.AsyncClient(
                    transport=self._transport, timeout=self._timeout
                ) as client:
                    resp = await client.get(
                        URL,
                        params={"function": "DIVIDENDS", "symbol": symbol, "apikey": self._api_key},
                    )
            except httpx.TransportError as err:
                exc = err
                continue
            if resp.status_code != 200:
                return FetchResult(ok=False, error=f"http_status:{resp.status_code}")
            try:
                payload = resp.json()
            except ValueError:
                return FetchResult(ok=False, error="invalid_json")
            return parse_dividends(payload, self._clock().date())
        return FetchResult(ok=False, error=f"network:{type(exc).__name__}")
