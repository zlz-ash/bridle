#!/usr/bin/env python3
"""Execution-scoped controller state shared across stream handling and publish."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def _new_lease_registry():
    from run_lease import RunLeaseRegistry

    return RunLeaseRegistry()


@dataclass
class ControllerExecutionContext:
    candidate_root: Path
    lease_id: str | None = None
    controller_ipc_dir: Path | None = None
    issued_it_run_id: str | None = None
    sentinel_by_handle: dict[str, Any] = field(default_factory=dict)
    handled_request_ids: set[str] = field(default_factory=set)
    lease_registry: Any = field(default_factory=_new_lease_registry)
