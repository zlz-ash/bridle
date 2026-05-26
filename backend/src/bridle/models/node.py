"""NodeRecord ORM model."""
from __future__ import annotations

from sqlalchemy import ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from bridle.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

# Use JSON for structured fields
from sqlalchemy import JSON


class NodeRecord(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "nodes"

    plan_id: Mapped[str] = mapped_column(ForeignKey("plans.id"), nullable=False)
    plan_node_id: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    goal: Mapped[str] = mapped_column(Text, nullable=False)
    node_type: Mapped[str] = mapped_column(String(50), nullable=False)
    order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    depends_on: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    files: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    tests: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    metrics: Mapped[dict | list] = mapped_column(JSON, nullable=False, default=dict)
    constraints: Mapped[dict | list] = mapped_column(JSON, nullable=False, default=dict)
    review_checks: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    expected_outputs: Mapped[dict | list] = mapped_column(JSON, nullable=False, default=dict)
    interfaces: Mapped[dict] = mapped_column(JSON, nullable=False, default=lambda: {"exposes": [], "consumes": []})
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="pending")

    plan: Mapped["PlanRecord"] = relationship("PlanRecord", back_populates="nodes")
    runs: Mapped[list["RunRecord"]] = relationship("RunRecord", back_populates="node", cascade="all, delete-orphan")
    proposals: Mapped[list["ProposalRecord"]] = relationship("ProposalRecord", back_populates="node", cascade="all, delete-orphan")


from bridle.models.run import RunRecord  # noqa: E402
from bridle.models.proposal import ProposalRecord  # noqa: E402

RunRecord
ProposalRecord
