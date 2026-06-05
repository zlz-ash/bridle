"""ChatMessageRecord ORM model."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from bridle.models.base import Base, UUIDPrimaryKeyMixin
from bridle.utils.datetime_util import utc_now_naive


class ChatMessageRecord(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "chat_messages"

    session_id: Mapped[str] = mapped_column(ForeignKey("agent_coding_sessions.id"), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    tool_calls: Mapped[list[dict] | None] = mapped_column(JSON, nullable=True)
    tool_result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utc_now_naive)

    session: Mapped["AgentCodingSessionRecord"] = relationship(
        "AgentCodingSessionRecord",
        back_populates="chat_messages",
    )


from bridle.models.agent_coding_session import AgentCodingSessionRecord  # noqa: E402

AgentCodingSessionRecord
