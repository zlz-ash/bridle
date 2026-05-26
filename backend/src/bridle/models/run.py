"""RunRecord ORM model."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from bridle.models.base import Base, UUIDPrimaryKeyMixin


class RunRecord(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "runs"

    node_id: Mapped[str] = mapped_column(ForeignKey("nodes.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="started")
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    stdout_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    stderr_path: Mapped[str | None] = mapped_column(Text, nullable=True)

    node: Mapped["NodeRecord"] = relationship("NodeRecord", back_populates="runs")
    evidences: Mapped[list["EvidenceRecord"]] = relationship(
        "EvidenceRecord", back_populates="run", cascade="all, delete-orphan"
    )


from bridle.models.evidence import EvidenceRecord  # noqa: E402

EvidenceRecord
