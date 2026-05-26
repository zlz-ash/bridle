"""NodeAgentResultRecord ORM model."""
from __future__ import annotations

from sqlalchemy import Float, ForeignKey, String, Text
from sqlalchemy import JSON
from sqlalchemy.orm import Mapped, mapped_column, relationship

from bridle.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class NodeAgentResultRecord(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "node_agent_results"

    run_id: Mapped[str] = mapped_column(ForeignKey("node_agent_runs.id"), nullable=False)
    node_id: Mapped[str] = mapped_column(String(36), nullable=False)
    status: Mapped[str] = mapped_column(String(50), nullable=False)
    result_type: Mapped[str] = mapped_column(String(50), nullable=False)
    proposal_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    issues: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    recommended_next_action: Mapped[str | None] = mapped_column(String(100), nullable=True)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    run: Mapped["NodeAgentRunRecord"] = relationship("NodeAgentRunRecord", back_populates="results")


from bridle.models.node_agent_run import NodeAgentRunRecord  # noqa: E402

NodeAgentRunRecord
