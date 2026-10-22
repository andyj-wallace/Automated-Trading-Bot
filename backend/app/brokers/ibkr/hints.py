"""What to tell the operator when IB Gateway is unreachable."""

from datetime import datetime, time, timedelta

WEEKLY_LOGIN = "Weekly IBKR login: open IBKR Mobile and tap Approve."
DAILY_RESTART = "Daily Gateway restart in progress; it reconnects by itself within a few minutes."
GENERAL = (
    "1) Check IBKR Mobile for a login request and tap Approve. "
    "2) No request? Restart the Gateway: scripts/bot.sh restart-gateway. "
    "3) Still down: open the Gateway screen over VNC (DEPLOYMENT.md)."
)

_SUNDAY = 6


def login_hint(now_et: datetime, restart_time: time) -> str:
    """Most likely cause of an outage at `now_et`, given the Gateway's daily restart time (ET)."""
    since_restart = now_et - datetime.combine(now_et.date(), restart_time, now_et.tzinfo)
    if timedelta(0) <= since_restart < timedelta(minutes=60) and now_et.weekday() == _SUNDAY:
        return WEEKLY_LOGIN
    if timedelta(0) <= since_restart < timedelta(minutes=15):
        return DAILY_RESTART
    return GENERAL
