"""EvidenceRecord ORM model."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, JSON, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from bridle.models.base import Base, UUIDPrimaryKeyMixin


class EvidenceRecord(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "evidences"

    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), nullable=False)
    node_id: Mapped[str] = mapped_column(ForeignKey("nodes.id"), nullable=False)
    evidence_type: Mapped[str] = mapped_column(String(50), nullable=False)
    content: Mapped[dict | list] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="collected")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.now)

    run: Mapped["RunRecord"] = relationship("RunRecord", back_populates="evidences")
