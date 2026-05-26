"""NodeAgentRunRecord ORM model."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from bridle.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class NodeAgentRunRecord(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "node_agent_runs"

    session_id: Mapped[str] = mapped_column(ForeignKey("agent_coding_sessions.id"), nullable=False)
    node_id: Mapped[str] = mapped_column(ForeignKey("nodes.id"), nullable=False)
    plan_node_id: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="queued")
    phase: Mapped[str] = mapped_column(String(50), nullable=False, default="initializing")
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    timeout_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    blocked_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    result_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    logs_ref: Mapped[str | None] = mapped_column(String(500), nullable=True)
    container_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    container_status: Mapped[str | None] = mapped_column(String(50), nullable=True)
    container_health: Mapped[str | None] = mapped_column(String(50), nullable=True)
    container_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    container_logs_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    diagnostic_path: Mapped[str | None] = mapped_column(String(500), nullable=True)

    session: Mapped["AgentCodingSessionRecord"] = relationship(
        "AgentCodingSessionRecord", back_populates="node_runs"
    )
    heartbeats: Mapped[list["NodeAgentHeartbeatRecord"]] = relationship(
        "NodeAgentHeartbeatRecord",
        back_populates="run",
        cascade="all, delete-orphan",
    )
    results: Mapped[list["NodeAgentResultRecord"]] = relationship(
        "NodeAgentResultRecord",
        back_populates="run",
        cascade="all, delete-orphan",
    )


from bridle.models.agent_coding_session import AgentCodingSessionRecord  # noqa: E402
from bridle.models.node_agent_heartbeat import NodeAgentHeartbeatRecord  # noqa: E402
from bridle.models.node_agent_result import NodeAgentResultRecord  # noqa: E402

AgentCodingSessionRecord
NodeAgentHeartbeatRecord
NodeAgentResultRecord
