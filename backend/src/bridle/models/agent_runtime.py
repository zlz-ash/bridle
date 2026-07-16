"""Persistent runtime facts owned by the application database."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from bridle.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class AgentRuntimeRecord(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Persist one agent runtime generation independently from conversation memory."""

    __tablename__ = "agent_runtimes"
    __table_args__ = (
        UniqueConstraint("agent_id", "generation", name="uq_agent_runtimes_agent_generation"),
    )

    runtime_type: Mapped[str] = mapped_column(String(50), nullable=False)
    owner_type: Mapped[str] = mapped_column(String(50), nullable=False)
    owner_id: Mapped[str] = mapped_column(String(200), nullable=False)
    project_id: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    session_id: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    parent_agent_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    parent_runtime_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    agent_id: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(50), nullable=False)
    status_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    result_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)


class RuntimeInputDeliveryRecord(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Persist the atomic link from a session message to one runtime input delivery."""

    __tablename__ = "runtime_input_deliveries"

    message_id: Mapped[str] = mapped_column(String(200), nullable=False, unique=True, index=True)
    session_message_id: Mapped[str] = mapped_column(String(200), nullable=False)
    project_id: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    session_id: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    target_address: Mapped[str] = mapped_column(String(1000), nullable=False)
    target_agent_id: Mapped[str] = mapped_column(String(200), nullable=False)
    target_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="pending")
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    mail_enqueued_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class RuntimeInputResultRecord(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Persist one handled runtime-input result without changing the delivery schema."""

    __tablename__ = "runtime_input_results"

    message_id: Mapped[str] = mapped_column(String(200), nullable=False, unique=True, index=True)
    assistant_message_id: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(50), nullable=False)
    handled_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class RuntimeChildResultReceiptRecord(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Persist completion of one child-result delivery across coordinator restarts."""

    __tablename__ = "runtime_child_result_receipts"

    message_id: Mapped[str] = mapped_column(String(200), nullable=False, unique=True, index=True)
    project_id: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    agent_id: Mapped[str] = mapped_column(String(200), nullable=False)
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    delivered_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
