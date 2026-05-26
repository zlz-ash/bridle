"""TaskRecord ORM model."""
from __future__ import annotations

from sqlalchemy import String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from bridle.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class TaskRecord(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "tasks"

    title: Mapped[str] = mapped_column(String(500), nullable=False)
    goal: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="created")

    plan: Mapped[PlanRecord | None] = relationship("PlanRecord", back_populates="task", uselist=False)


from bridle.models.plan import PlanRecord  # noqa: E402

PlanRecord  # ensure import for relationship resolution
