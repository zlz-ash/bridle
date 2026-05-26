"""Evidence schemas."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class EvidenceReadSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    run_id: str
    node_id: str
    evidence_type: str
    content: dict | list
    status: str
    created_at: datetime
