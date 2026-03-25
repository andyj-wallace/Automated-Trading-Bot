import enum
from datetime import datetime

from sqlalchemy import BigInteger, Enum as SAEnum, Text, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class LogCategory(str, enum.Enum):
    TRADING = "TRADING"
    RISK = "RISK"
    SYSTEM = "SYSTEM"
    ERROR = "ERROR"


class SystemLog(Base):
    """
    Structured log entry persisted to the database for the logs viewer UI.

    Uses BIGSERIAL PK (not UUID) per design.md § system_logs.
    Note: This table supplements (does not replace) the file-based log streams.
    """

    __tablename__ = "system_logs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    level: Mapped[str] = mapped_column(String(20), nullable=False)
    category: Mapped[LogCategory] = mapped_column(
        SAEnum(LogCategory, name="logcategory"), nullable=False, index=True
    )
    message: Mapped[str] = mapped_column(Text, nullable=False)
    context: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default=func.now(), index=True
    )

    def __repr__(self) -> str:
        return f"<SystemLog [{self.level}] {self.category}: {self.message[:50]}>"
