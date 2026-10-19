import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.config import get_settings
from app.dependencies import get_engine
from app.main import app


def test_tests_run_without_engine_and_with_temp_log_dir():
    s = get_settings()
    assert s.engine_enabled is False
    assert s.log_dir != "logs"


def test_client_startup_has_no_engine(client):
    assert getattr(app.state, "engine", None) is None


def test_get_engine_raises_503_when_missing():
    request = Request({"type": "http", "app": app, "headers": []})
    with pytest.raises(HTTPException) as exc:
        get_engine(request)
    assert exc.value.status_code == 503
