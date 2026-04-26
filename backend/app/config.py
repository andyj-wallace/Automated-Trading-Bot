from functools import lru_cache

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
    ibkr_port: int = 4001
    ibkr_client_id: int = 1
    ibkr_username: str = ""
    ibkr_password: str = ""
    ibkr_trading_mode: str = "paper"

    # Broker selection
    broker: str = "mock"  # "mock" | "ibkr"

    # Notifications (Phase 3+)
    notification_email_smtp: str = ""
    # Twilio SMS: twilio://account_sid:auth_token@+from_number/+to_number
    notification_sms_twilio: str = ""

    # API binding (production should use 127.0.0.1)
    api_host: str = "127.0.0.1"
    api_port: int = 8000

    # VNC (cloud deployment only)
    vnc_password: str = ""

    @property
    def is_development(self) -> bool:
        return self.environment == "development"

    @property
    def is_production(self) -> bool:
        return self.environment == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()
