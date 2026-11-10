"""Connection state, reconnect listeners and gateway event handlers for IBKRClient."""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from app.brokers.base import BrokerConnectionInfo
from app.brokers.ibkr.hints import login_hint
from app.monitoring.logger import system_logger


class IBKRConnectionStatusMixin:
    """Connection-state reporting and gateway event handlers mixed into IBKRClient."""

    def add_reconnect_listener(self, fn: Callable[[], Awaitable[None]]) -> None:
        """Register a coroutine function awaited after every successful reconnect."""
        self._reconnect_listeners.append(fn)

    def _now(self) -> datetime:
        return self._clock().astimezone(UTC)

    def _mark_down(self, exc: BaseException | None = None) -> None:
        if self._down_since is None:
            self._down_since = self._now()
        if exc is not None:
            self._last_error = str(exc) or type(exc).__name__

    def _hint(self) -> str:
        return login_hint(self._clock(), self._settings.ib_auto_restart_time)

    def connection_info(self) -> BrokerConnectionInfo:
        """Connection state plus the most likely fix, for the dashboard and health check."""
        if self._ib.isConnected():
            return BrokerConnectionInfo(state="connected", last_connected_at=self._last_connected_at)
        if self._account_mismatch:
            state = "account_mismatch"
            hint = "The Gateway is logged into a non-paper account; log it into the paper account."
        else:
            down_for = (self._now() - self._down_since).total_seconds() if self._down_since else 0
            state = "down" if down_for >= self._alert_after else "reconnecting"
            hint = self._hint()
        return BrokerConnectionInfo(
            state=state,
            down_since=self._down_since,
            last_connected_at=self._last_connected_at,
            last_error=self._last_error,
            hint=hint,
        )

    def _on_disconnected(self) -> None:
        """Fired by ib_async when the Gateway drops the connection; the supervisor reconnects."""
        system_logger.warning(
            "IBKRClient lost connection to IB Gateway",
            extra={"host": self._settings.ibkr_host, "port": self._settings.ibkr_port},
        )

    def _on_error(self, req_id: int, error_code: int, error_string: str, contract) -> None:
        """
        Fired by ib_async for every error or informational message from IB Gateway.

        Error codes 1100-1300 relate to connectivity. Key ones:
          1100 — connection lost
          1101 — reconnected, data lost
          1102 — reconnected, data maintained
          2110 — connectivity between TWS and server is broken
        See: https://ibkrcampus.com/ibkr-api-page/trader-workstation-api/#error-codes
        """
        # IB uses low codes (1-9xx) for normal info messages; only log as warning/error for real problems
        if error_code < 2000:
            system_logger.warning(
                "IBKRClient: IB Gateway error",
                extra={
                    "req_id": req_id,
                    "error_code": error_code,
                    "error": error_string,
                    "symbol": contract.symbol if contract else None,
                },
            )
        else:
            system_logger.info(
                "IBKRClient: IB Gateway message",
                extra={
                    "req_id": req_id,
                    "error_code": error_code,
                    "ib_message": error_string,
                },
            )
