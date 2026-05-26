"""AgentCodingSessionRecord ORM model."""
from __future__ import annotations

from sqlalchemy import ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from bridle.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class AgentCodingSessionRecord(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "agent_coding_sessions"

    plan_id: Mapped[str] = mapped_column(ForeignKey("plans.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="active")
    mode: Mapped[str] = mapped_column(String(50), nullable=False, default="coding")
    auto_continue_budget: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    auto_continue_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    node_runs: Mapped[list["NodeAgentRunRecord"]] = relationship(
        "NodeAgentRunRecord",
        back_populates="session",
        cascade="all, delete-orphan",
    )


from bridle.models.node_agent_run import NodeAgentRunRecord  # noqa: E402

NodeAgentRunRecord
