#!/usr/bin/env python3
"""Controller-issued run lease for Docker integration teardown authority."""
from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path

LOGGER = logging.getLogger("bridle.run_lease")

RUN_REGISTER_PREFIX = "BRIDLE_RUN_REGISTER:"


@dataclass
class RunLease:
    lease_id: str
    candidate_root: str
    registered_it_run_ids: set[str] = field(default_factory=set)


class RunLeaseRegistry:
    def __init__(self) -> None:
        self._leases: dict[str, RunLease] = {}

    def create_lease(self, *, candidate_root: Path, ipc_dir: Path) -> RunLease:
        lease_id = uuid.uuid4().hex
        lease = RunLease(lease_id=lease_id, candidate_root=str(candidate_root.resolve()))
        self._leases[lease_id] = lease
        path = ipc_dir / "run-lease.json"
        path.write_text(
            json.dumps(
                {
                    "lease_id": lease_id,
                    "candidate_root": lease.candidate_root,
                    "registered_it_run_ids": [],
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        LOGGER.info("run_lease_created lease_id=%s candidate_root=%s", lease_id, lease.candidate_root)
        return lease

    def load_lease(self, lease_id: str) -> RunLease:
        lease = self._leases.get(lease_id)
        if lease is None:
            raise RuntimeError(f"run_lease_unknown lease_id={lease_id}")
        return lease

    def register_it_run_id(self, lease_id: str, it_run_id: str, *, ipc_dir: Path) -> None:
        text = it_run_id.strip()
        if not text:
            raise RuntimeError("run_register_empty_it_run_id")
        lease = self.load_lease(lease_id)
        lease.registered_it_run_ids.add(text)
        self._persist(lease, ipc_dir=ipc_dir)
        LOGGER.info("run_lease_registered_it_run lease_id=%s it_run_id=%s", lease_id, text)

    def assert_teardown_allowed(self, lease_id: str, it_run_id: str) -> None:
        lease = self.load_lease(lease_id)
        text = it_run_id.strip()
        if text not in lease.registered_it_run_ids:
            raise RuntimeError(f"run_teardown_foreign_it_run_id it_run_id={text}")

    def _persist(self, lease: RunLease, *, ipc_dir: Path) -> None:
        path = ipc_dir / "run-lease.json"
        path.write_text(
            json.dumps(
                {
                    "lease_id": lease.lease_id,
                    "candidate_root": lease.candidate_root,
                    "registered_it_run_ids": sorted(lease.registered_it_run_ids),
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )


_REGISTRY = RunLeaseRegistry()


def get_registry() -> RunLeaseRegistry:
    return _REGISTRY


def handle_run_register_line(line: str, *, lease_id: str, ipc_dir: Path) -> None:
    if not line.startswith(RUN_REGISTER_PREFIX):
        return
    payload = json.loads(line[len(RUN_REGISTER_PREFIX) :])
    it_run_id = str(payload.get("it_run_id") or "")
    _REGISTRY.register_it_run_id(lease_id, it_run_id, ipc_dir=ipc_dir)
