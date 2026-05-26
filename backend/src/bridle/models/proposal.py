"""ProposalRecord ORM model — agent dry-run proposal persistence."""
from __future__ import annotations

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from bridle.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from sqlalchemy import JSON


class ProposalRecord(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "proposals"

    node_id: Mapped[str] = mapped_column(ForeignKey("nodes.id"), nullable=False)
    plan_node_id: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    instruction: Mapped[str] = mapped_column(Text, nullable=False)
    allowed_files: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    accessible_context: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    proposal: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="proposed")
    source: Mapped[str] = mapped_column(String(50), nullable=False, default="agent")

    node: Mapped["NodeRecord"] = relationship("NodeRecord", back_populates="proposals")


from bridle.models.node import NodeRecord  # noqa: E402
NodeRecord  # prevent unused-import
