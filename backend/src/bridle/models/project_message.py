"""Unified project session message ORM model."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from bridle.models.base import Base, UUIDPrimaryKeyMixin
from bridle.utils.datetime_util import utc_now_naive


class ProjectMessageRecord(UUIDPrimaryKeyMixin, Base):
    """Persist one conversation message; session/message input exits as ordered history."""

    __tablename__ = "project_messages"

    session_id: Mapped[str] = mapped_column(ForeignKey("project_sessions.id"), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    tool_calls: Mapped[list[dict] | None] = mapped_column(JSON, nullable=True)
    tool_result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utc_now_naive)

    session: Mapped["ProjectSessionRecord"] = relationship(
        "ProjectSessionRecord",
        back_populates="messages",
    )


from bridle.models.project_session import ProjectSessionRecord  # noqa: E402

ProjectSessionRecord
