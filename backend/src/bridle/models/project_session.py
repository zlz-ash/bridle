"""Unified planning/execution session ORM model."""
from __future__ import annotations

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from bridle.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class ProjectSessionRecord(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Persist one project conversation; project input exits with shared role and history state."""

    __tablename__ = "project_sessions"

    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    project_path_snapshot: Mapped[str] = mapped_column(String(2000), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False, default="New conversation")
    role: Mapped[str] = mapped_column(String(20), nullable=False, default="planning")
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="active")

    project: Mapped["ProjectRecord"] = relationship("ProjectRecord", back_populates="sessions")
    messages: Mapped[list["ProjectMessageRecord"]] = relationship(
        "ProjectMessageRecord",
        back_populates="session",
        cascade="all, delete-orphan",
    )


from bridle.models.project import ProjectRecord  # noqa: E402
from bridle.models.project_message import ProjectMessageRecord  # noqa: E402

ProjectRecord
ProjectMessageRecord
