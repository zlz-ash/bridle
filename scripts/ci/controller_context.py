#!/usr/bin/env python3
"""Execution-scoped controller state shared across stream handling and publish."""
from __future__ import annotations

import importlib.util
import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def _load_run_lease_module():
    import sys

    script_dir = Path(__file__).resolve().parent
    spec = importlib.util.spec_from_file_location("run_lease", script_dir / "run_lease.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _new_lease_registry():
    return _load_run_lease_module().RunLeaseRegistry()


def issue_critical_test_nonces(ctx: Any) -> dict[str, str]:
    """Mint one-time nonces for each critical test key, stored on the context."""
    import hashlib

    nonces: dict[str, str] = {}
    for test_key in ("link_attack", "chmod_poison"):
        material = f"{test_key}:{uuid.uuid4().hex}".encode("utf-8")
        nonces[test_key] = hashlib.sha256(material).hexdigest()[:32]
    ctx.critical_test_nonces = dict(nonces)
    return nonces


def nonces_env_payload(ctx: Any) -> str:
    return json.dumps(ctx.critical_test_nonces, sort_keys=True)


@dataclass
class ControllerExecutionContext:
    candidate_root: Path
    lease_id: str | None = None
    controller_ipc_dir: Path | None = None
    issued_it_run_id: str | None = None
    sentinel_by_handle: dict[str, Any] = field(default_factory=dict)
    verified_sentinel_by_request: dict[str, Any] = field(default_factory=dict)
    consumed_sentinel_verification_requests: set[str] = field(default_factory=set)
    handled_request_ids: set[str] = field(default_factory=set)
    lease_registry: Any = field(default_factory=_new_lease_registry)
    isolated_docker_host: str | None = None
    isolated_dind_name: str | None = None
    isolated_network: str | None = None
    critical_test_nonces: dict[str, str] = field(default_factory=dict)
    consumed_test_event_keys: set[str] = field(default_factory=set)
