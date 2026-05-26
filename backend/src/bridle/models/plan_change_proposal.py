"""PlanChangeProposalRecord ORM model."""
from __future__ import annotations

from sqlalchemy import Boolean, ForeignKey, String, Text, JSON
from sqlalchemy.orm import Mapped, mapped_column

from bridle.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class PlanChangeProposalRecord(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "plan_change_proposals"

    plan_id: Mapped[str] = mapped_column(ForeignKey("plans.id"), nullable=False)
    proposal_type: Mapped[str] = mapped_column(String(50), nullable=False, default="plan_change")
    change_set: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    risk_level: Mapped[str] = mapped_column(String(50), nullable=False, default="low")
    requires_human_review: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="proposed")
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
