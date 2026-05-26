"""Common enums and shared schema fields."""
from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel


class ApiError(BaseModel):
    """Unified error response format for all API endpoints."""

    code: str
    message: str
    details: dict | None = None
    resource: str | None = None


class NodeType(StrEnum):
    CODE_CHANGE = "code_change"
    TEST_VALIDATION = "test_validation"
    METRIC_VALIDATION = "metric_validation"
    REVIEW_GATE = "review_gate"


class TaskStatus(StrEnum):
    CREATED = "created"
    PLANNED = "planned"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class PlanStatus(StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    COMPLETED = "completed"
    FAILED = "failed"
    ARCHIVED = "archived"


class NodeStatus(StrEnum):
    PENDING = "pending"
    BLOCKED = "blocked"
    READY = "ready"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    FAILED_RETRYABLE = "failed_retryable"
    NEEDS_REVIEW = "needs_review"
    NEEDS_REVIEW_RETRYABLE = "needs_review_retryable"
    MISSING_EVIDENCE = "missing_evidence"
    ARCHIVED = "archived"


class RunStatus(StrEnum):
    STARTED = "started"
    COMPLETED = "completed"
    FAILED = "failed"


class EvidenceType(StrEnum):
    TEST_RESULT = "test_result"
    METRIC = "metric"
    LOG = "log"
    ARTIFACT = "artifact"


class EvidenceStatus(StrEnum):
    COLLECTED = "collected"
    MISSING_EVIDENCE = "missing_evidence"
