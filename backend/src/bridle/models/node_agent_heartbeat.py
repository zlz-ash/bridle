"""NodeAgentHeartbeatRecord ORM model."""
from __future__ import annotations

from sqlalchemy import Float, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from bridle.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class NodeAgentHeartbeatRecord(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "node_agent_heartbeats"

    run_id: Mapped[str] = mapped_column(ForeignKey("node_agent_runs.id"), nullable=False)
    node_id: Mapped[str] = mapped_column(String(36), nullable=False)
    status: Mapped[str] = mapped_column(String(50), nullable=False)
    phase: Mapped[str] = mapped_column(String(50), nullable=False, default="")
    message: Mapped[str] = mapped_column(Text, nullable=False, default="")
    progress: Mapped[float | None] = mapped_column(Float, nullable=True)

    run: Mapped["NodeAgentRunRecord"] = relationship("NodeAgentRunRecord", back_populates="heartbeats")


from bridle.models.node_agent_run import NodeAgentRunRecord  # noqa: E402

NodeAgentRunRecord
