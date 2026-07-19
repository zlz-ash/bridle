"""Persisted checkpoint for one project's dynamic short-term memory window."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from bridle.models.base import Base
from bridle.utils.datetime_util import utc_now_naive


class ProjectSessionMemoryRecord(Base):
    """Store the optimized summary and the last message represented by it."""

    __tablename__ = "project_session_memories"

    session_id: Mapped[str] = mapped_column(
        ForeignKey("project_sessions.id", ondelete="CASCADE"),
        primary_key=True,
    )
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    anchor_message_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=utc_now_naive,
        onupdate=utc_now_naive,
    )
