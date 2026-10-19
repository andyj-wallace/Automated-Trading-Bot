"""TradingState: the operator pause switch for automated entries, stored in Redis."""

from __future__ import annotations

from app.monitoring.logger import system_logger

PAUSED_KEY = "trading:paused"


class TradingState:
    """Pause blocks automated entries only; exits, monitor and reconciler keep running."""

    def __init__(self, cache) -> None:
        self._cache = cache

    async def is_paused(self) -> bool:
        """True when paused, or when Redis is unreachable (fail safe for entries)."""
        try:
            return (await self._cache.get(PAUSED_KEY)) == "1"
        except Exception as exc:
            system_logger.warning(
                "TradingState: Redis unreachable, treating as paused", extra={"error": str(exc)}
            )
            return True

    async def pause(self, reason: str = "") -> None:
        await self._cache.set(PAUSED_KEY, "1")
        system_logger.warning("Automated trading paused", extra={"reason": reason})

    async def resume(self) -> None:
        await self._cache.delete(PAUSED_KEY)
        system_logger.warning("Automated trading resumed")
