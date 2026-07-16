"""Registered project ORM model."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from bridle.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from bridle.utils.datetime_util import utc_now_naive


class ProjectRecord(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Persist one canonical project path; inputs are path/name and output is a reusable project ID."""

    __tablename__ = "projects"

    path: Mapped[str] = mapped_column(String(2000), nullable=False, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(500), nullable=False)
    last_opened_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utc_now_naive)

    sessions: Mapped[list[ProjectSessionRecord]] = relationship(
        "ProjectSessionRecord",
        back_populates="project",
        cascade="all, delete-orphan",
    )


from bridle.models.project_session import ProjectSessionRecord  # noqa: E402
