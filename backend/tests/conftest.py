import os
import tempfile

os.environ["ENGINE_ENABLED"] = "false"
os.environ.setdefault("LOG_DIR", tempfile.mkdtemp(prefix="tradingbot-test-logs-"))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c
