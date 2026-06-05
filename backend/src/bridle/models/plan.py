"""PlanRecord ORM model."""
from __future__ import annotations

from sqlalchemy import ForeignKey, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from bridle.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class PlanRecord(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "plans"

    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), nullable=False)
    goal: Mapped[str] = mapped_column(Text, nullable=False)
    aggregate_files: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="draft")

    task: Mapped["TaskRecord"] = relationship("TaskRecord", back_populates="plan")
    nodes: Mapped[list["NodeRecord"]] = relationship("NodeRecord", back_populates="plan", cascade="all, delete-orphan")


from bridle.models.node import NodeRecord  # noqa: E402

NodeRecord
