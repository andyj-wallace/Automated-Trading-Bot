"""TradingEngine.start() when the Gateway is logged into the wrong account."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.brokers.ibkr.client import LiveTradingGuardError
from app.core.engine import TradingEngine


async def test_account_mismatch_at_startup_is_emailed_directly():
    engine = TradingEngine.__new__(TradingEngine)  # skip __init__: no DB, Redis or broker needed
    engine._seed_strategies = AsyncMock()
    engine.broker = SimpleNamespace(connect=AsyncMock(side_effect=LiveTradingGuardError("live account")))
    engine.cache = None
    engine.dispatcher = SimpleNamespace(notify_system_alert=AsyncMock())

    await engine.start()

    engine.dispatcher.notify_system_alert.assert_awaited_once_with(
        "BROKER_ACCOUNT_MODE_MISMATCH", "live account"
    )
