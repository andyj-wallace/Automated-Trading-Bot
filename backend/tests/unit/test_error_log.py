"""D4: error.log has one writer and collects ERROR from every application logger."""

import json
import logging

import pytest

from app.monitoring import logger as log_mod

NAMES = ("trading", "risk", "system", "error", "audit")


@pytest.fixture
def fresh_logging(tmp_path):
    saved = {n: list(logging.getLogger(n).handlers) for n in NAMES}
    for n in NAMES:
        logging.getLogger(n).handlers = []
    was = log_mod._CONFIGURED
    log_mod._CONFIGURED = False
    yield tmp_path
    for n in NAMES:
        for h in logging.getLogger(n).handlers:
            h.close()
        logging.getLogger(n).handlers = saved[n]
    log_mod._CONFIGURED = was


def test_errors_from_all_loggers_land_in_one_error_log(fresh_logging):
    log_mod.setup_logging("INFO", str(fresh_logging))
    logging.getLogger("system").error("sys boom", extra={"k": 1})
    logging.getLogger("trading").error("trade boom")
    logging.getLogger("risk").error("risk boom")
    logging.getLogger("error").error("err boom")
    logging.getLogger("system").info("not an error")
    for n in NAMES:
        for h in logging.getLogger(n).handlers:
            h.flush()
    lines = [json.loads(x) for x in (fresh_logging / "error.log").read_text().splitlines()]
    assert [x["message"] for x in lines] == ["sys boom", "trade boom", "risk boom", "err boom"]
    file_handlers = {
        h
        for n in NAMES
        for h in logging.getLogger(n).handlers
        if getattr(h, "baseFilename", "").endswith("error.log")
    }
    assert len(file_handlers) == 1
