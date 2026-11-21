"""Unit tests for the Alpha Vantage DIVIDENDS client. No network: httpx.MockTransport only."""

import json
import logging
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import httpx

from app.core import market_calendar
from app.data.alpha_vantage import AlphaVantageClient, parse_dividends

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "alpha_vantage_dividends_ibm.json").read_text()
)
KEY = "SECRET-KEY-123"
TODAY = date(2026, 11, 1)


def clock() -> datetime:
    return datetime(2026, 11, 1, 10, tzinfo=market_calendar.ET)


async def allow() -> bool:
    return True


def make_client(handler) -> AlphaVantageClient:
    return AlphaVantageClient(KEY, transport=httpx.MockTransport(handler), clock=clock)


async def test_fixture_reply_parses_rows():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["function"] == "DIVIDENDS"
        return httpx.Response(200, json=FIXTURE)

    result = await make_client(handler).fetch("IBM", allow)

    assert result.ok is True
    assert len(result.rows) == 3
    assert result.dropped == 0
    assert result.rows[0].amount == Decimal("1.69")
    assert result.rows[0].declared is True
    assert result.rows[1].declared is False
    assert result.rows[1].declaration_date is None
    assert result.rows[0].payment_date == date(2026, 12, 10)


def test_parse_drops_malformed_rows():
    payload = {
        "data": [
            {"ex_dividend_date": "None", "amount": "1.00"},
            {"ex_dividend_date": "2026-05-09", "amount": ""},
            {"ex_dividend_date": "2026-05-09", "amount": "abc"},
            {"ex_dividend_date": "2026-05-09", "amount": "0"},
            "not a dict",
            {"ex_dividend_date": "2026-05-09", "amount": "0.50"},
        ]
    }

    result = parse_dividends(payload, TODAY)

    assert result.ok is True
    assert len(result.rows) == 1
    assert result.dropped == 5


def test_parse_rejects_missing_data_list():
    assert parse_dividends({"Information": "rate limit"}, TODAY).error == "no_data_list"
    assert parse_dividends("<html>", TODAY).error == "no_data_list"


async def test_rate_limit_body_fails():
    result = await make_client(
        lambda request: httpx.Response(200, json={"Information": "rate limit"})
    ).fetch("IBM", allow)

    assert result.ok is False


async def test_html_body_fails_as_invalid_json():
    result = await make_client(
        lambda request: httpx.Response(200, text="<html>")
    ).fetch("IBM", allow)

    assert result.ok is False
    assert result.error == "invalid_json"


async def test_status_500_fails_without_retry():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(500)

    result = await make_client(handler).fetch("IBM", allow)

    assert result.ok is False
    assert result.error == "http_status:500"
    assert len(calls) == 1


async def test_timeout_every_time_fails_after_two_attempts():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise httpx.ReadTimeout("t", request=request)

    result = await make_client(handler).fetch("IBM", allow)

    assert result.ok is False
    assert result.error == "network:ReadTimeout"
    assert len(calls) == 2


async def test_connect_error_then_success_is_ok():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            raise httpx.ConnectError("refused", request=request)
        return httpx.Response(200, json=FIXTURE)

    result = await make_client(handler).fetch("IBM", allow)

    assert result.ok is True
    assert len(result.rows) == 3


async def test_budget_denied_sends_no_request():
    calls = []

    async def deny() -> bool:
        return False

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=FIXTURE)

    result = await make_client(handler).fetch("IBM", deny)

    assert result.error == "budget_exhausted"
    assert calls == []


async def test_budget_runs_out_before_retry():
    calls = []
    allowed = iter([True, False])

    async def may_request() -> bool:
        return next(allowed)

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise httpx.ConnectError("refused", request=request)

    result = await make_client(handler).fetch("IBM", may_request)

    assert result.ok is False
    assert result.error == "budget_exhausted"
    assert len(calls) == 1


async def test_key_never_leaks_into_error_or_logs(caplog):
    caplog.set_level(logging.DEBUG)

    result = await make_client(lambda request: httpx.Response(500)).fetch("IBM", allow)

    assert KEY not in (result.error or "")
    assert KEY not in caplog.text

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"failed for {request.url}", request=request)

    failed = await make_client(boom).fetch("IBM", allow)
    assert KEY not in (failed.error or "")
