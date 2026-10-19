import re
from pathlib import Path

from ib_async import IB


def test_every_ib_method_used_by_client_exists():
    src = Path("app/brokers/ibkr/client.py").read_text()
    ib = IB()
    missing = [n for n in set(re.findall(r"self\._ib\.(\w+)", src)) if not hasattr(ib, n)]
    assert not missing, missing
