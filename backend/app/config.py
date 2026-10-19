from datetime import time
from decimal import Decimal
from functools import lru_cache

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

    @field_validator("max_portfolio_risk_pct")
    @classmethod
    def _cap_portfolio_risk(cls, value: Decimal) -> Decimal:
        if value <= 0 or value > Decimal("0.10"):
            raise ValueError("max_portfolio_risk_pct must be > 0 and <= 0.10")
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
