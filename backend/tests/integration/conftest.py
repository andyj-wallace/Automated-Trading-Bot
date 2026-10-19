import os

import pytest
from sqlalchemy.engine import make_url

_url = os.environ.get("DATABASE_URL")
if not _url or not (make_url(_url).database or "").endswith("_test"):
    raise pytest.UsageError(
        "Integration tests TRUNCATE tables. Export DATABASE_URL pointing at a database whose "
        "name ends in _test (see .claude/specs/production-readiness-plan.md section 0.2)."
    )
