# Import all models here so that:
# 1. Base.metadata has all table definitions when Alembic runs.
# 2. Any module that does `from app.db.models import ...` gets them all.

from app.db.models.portfolio import PortfolioSnapshot
from app.db.models.strategy import TradingStrategy
from app.db.models.system_log import LogCategory, SystemLog
from app.db.models.trade import Trade, TradeDirection, TradeStatus
from app.db.models.watched_symbol import WatchedSymbol

__all__ = [
    "PortfolioSnapshot",
    "TradingStrategy",
    "LogCategory",
    "SystemLog",
    "Trade",
    "TradeDirection",
    "TradeStatus",
    "WatchedSymbol",
]
