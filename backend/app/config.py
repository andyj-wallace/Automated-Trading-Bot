from datetime import datetime, time
from decimal import Decimal
from functools import lru_cache
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # Application
    environment: str = "development"
    log_level: str = "INFO"
    secret_key: str  # no default — must be set

    # Database
    database_url: str  # no default — must be set

    # Redis
    redis_url: str = "redis://localhost:6379/0"

    # IBKR
    ibkr_host: str = "127.0.0.1"
    ibkr_port: int = 4002
    ibkr_client_id: int = 1
    ibkr_username: str = ""
    ibkr_password: str = ""
    ibkr_trading_mode: str = "paper"
    # The Docker Gateway's daily restart (ET); Sunday's restart asks for the weekly IBKR Mobile approval.
    ib_auto_restart_time: time = time(19, 0)

    # Broker selection
    broker: str = "mock"  # "mock" | "ibkr"

    # Short selling — OFF by default. The risk/execution stack is currently
    # long-only by design (RiskCalculator/RiskManager reject any trade where
    # stop_loss_price >= entry_price). Flipping this on does NOT enable real
    # shorting yet: it routes into explicit NotImplementedError stubs in
    # risk/calculator.py and risk/manager.py so the gap stays visible rather
    # than silently mis-pricing risk. Real support needs: inverted PnL math
    # in OrderManager, inverted stop/target comparisons in PositionMonitor,
    # and broker-side margin/short-availability checks. Interim bear-market
    # response: BullBearStrategy's `inverse_etf_symbol` config (buys an
    # inverse ETF instead of shorting — see strategy_engine/bull_bear.py).
    enable_short_selling: bool = False

    # Notifications (Phase 3+)
    notification_email_smtp: str = ""
    # Twilio SMS: twilio://account_sid:auth_token@+from_number/+to_number
    notification_sms_twilio: str = ""

    # API binding (production should use 127.0.0.1)
    api_host: str = "127.0.0.1"
    api_port: int = 8000

    # VNC (cloud deployment only)
    vnc_password: str = ""

    # Trading engine
    engine_enabled: bool = True
    log_dir: str = "logs"
    cors_origins: list[str] = ["http://localhost:5173"]  # env value is JSON: '["https://x"]'
    scheduler_interval_seconds: int = 60
    strategy_eval_start_et: time = time(9, 45)
    strategy_eval_end_et: time = time(15, 45)
    bar_refresh_time_et: time = time(16, 30)
    daily_summary_time_et: time = time(16, 45)
    price_stale_seconds: int = 120

    # Risk. The 1% per-trade rule is deliberately NOT a setting.
    max_portfolio_risk_pct: Decimal = Decimal("0.05")
    min_reward_to_risk: Decimal = Decimal("2.0")
    daily_loss_limit_pct: Decimal = Decimal("0.03")
    max_consecutive_losses: int = 5
    stale_trade_max_days: int = 14

    # Execution / broker
    order_fill_timeout_seconds: int = 30
    broker_protective_stops: bool = True
    reconcile_interval_seconds: float = 120
    ibkr_market_data_type: int = 1  # 1 live, 3 delayed (paper only)

    # Small-account growth mode
    commission_plan: Literal["ibkr_fixed", "ibkr_tiered", "zero"] = "ibkr_fixed"
    slippage_bps: Decimal = Decimal("5")
    max_breakeven_win_rate: Decimal = Decimal("0.45")
    account_type: Literal["cash", "margin"] = "cash"
    trading_capital_limit: Decimal | None = None
    growth_mode_enabled: bool = True
    growth_mode_exit_equity: Decimal = Decimal("2000")
    growth_max_open_positions: int = 2
    growth_universe: list[str] = [
        "SPY", "QQQ", "IWM", "DIA", "SCHB", "SCHX", "SCHG",
        "SCHD", "SCHF", "SPDW", "SPEM", "XLF", "EEM", "VWO",
    ]
    max_spread_bps: Decimal = Decimal("20")
    min_avg_dollar_volume: Decimal = Decimal("20000000")

    @field_validator("max_breakeven_win_rate")
    @classmethod
    def _check_breakeven_rate(cls, value: Decimal) -> Decimal:
        if not Decimal("0") < value < Decimal("1"):
            raise ValueError("max_breakeven_win_rate must be between 0 and 1 (exclusive)")
        return value

    @field_validator("growth_max_open_positions")
    @classmethod
    def _check_growth_positions(cls, value: int) -> int:
        if value < 1:
            raise ValueError("growth_max_open_positions must be >= 1")
        return value

    @field_validator("growth_universe")
    @classmethod
    def _normalize_universe(cls, value: list[str]) -> list[str]:
        return [s.strip().upper() for s in value]

    @field_validator("max_portfolio_risk_pct")
    @classmethod
    def _cap_portfolio_risk(cls, value: Decimal) -> Decimal:
        if value <= 0 or value > Decimal("0.10"):
            raise ValueError("max_portfolio_risk_pct must be > 0 and <= 0.10")
        return value

    @field_validator("ib_auto_restart_time", mode="before")
    @classmethod
    def _parse_gateway_time(cls, value):
        """Accept the Gateway's own "07:00 PM" format as well as "19:00"."""
        if isinstance(value, str):
            try:
                return datetime.strptime(value.strip(), "%I:%M %p").time()
            except ValueError:
                pass
        return value

    @property
    def is_development(self) -> bool:
        return self.environment == "development"

    @property
    def is_production(self) -> bool:
        return self.environment == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()
