"""Prepare candidate workspaces from authoritative map execution snapshots."""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bridle.agent.container.boundary import compute_boundary_fingerprint
from bridle.agent.container.candidate_contract import CandidateExecutionRequest
from bridle.agent.container.candidate_path_guard import validate_safe_id
from bridle.agent.container.image_identity import resolve_image_identity
from bridle.agent.container.workset import MapInterfaceMock, MapWorksetInput
from bridle.agent.container.workspace import ContainerWorkspaceBuilder, ContainerWorkspaceResult


@dataclass(frozen=True)
class CandidateSetup:
    candidate_id: str
    request: CandidateExecutionRequest
    workspace: ContainerWorkspaceResult
    boundary_fingerprint: str
    module_id: str
    module_root: Path
    map_snapshot: dict[str, Any]


class CandidateExecutionService:
    """Build candidate directories and contracts from map execution snapshots."""

    DEFAULT_IMAGE = "bridle-agent:local"

    def __init__(self, project_root: str | Path) -> None:
        self.project_root = Path(project_root).resolve()

    def prepare_from_snapshot(
        self,
        snapshot: dict[str, Any],
        *,
        run_id: str,
        readonly_files: list[str] | None = None,
        candidate_id: str | None = None,
        network_allowed: bool = False,
        timeout_seconds: int = 300,
        base_map_seq: int = 0,
    ) -> CandidateSetup:
        if snapshot.get("error_code"):
            raise ValueError(f"{snapshot['error_code']}: {snapshot.get('detail')}")

        module_id = str(snapshot["module_id"])
        node_id = str(snapshot["node_id"])
        cid = validate_safe_id(candidate_id or f"cand-{uuid.uuid4().hex[:12]}", field="candidate_id")

        impl_files = tuple(entity["path"] for entity in snapshot.get("implementation_entities") or [])
        test_files = tuple(entity["path"] for entity in snapshot.get("test_entities") or [])
        test_commands = tuple(str(item) for item in snapshot.get("test_commands") or [] if str(item).strip())

        mocks: tuple[MapInterfaceMock, ...] = tuple(
            MapInterfaceMock(
                interface_id=str(item["interface_id"]),
                from_module=str(item["from_module"]),
                to_module=str(item["to_module"]),
                file_path=str(item["file_path"]),
                mock_hash=str(item["mock_hash"]),
                entity_version=str(item.get("entity_version") or item["mock_hash"]),
            )
            for item in snapshot.get("interfaces") or []
        )

        workset = MapWorksetInput(
            module_id=module_id,
            node_id=node_id,
            implementation_files=impl_files,
            test_files=test_files,
            test_commands=test_commands,
            interface_mocks=mocks,
            readonly_context=tuple(readonly_files or []),
            test_dir=snapshot.get("test_dir"),
        )
        workspace = ContainerWorkspaceBuilder(self.project_root).build_candidate_workspace(
            candidate_id=cid,
            module_id=module_id,
            run_id=run_id,
            node_id=node_id,
            workset=workset,
        )
        image_version = resolve_image_identity(self.DEFAULT_IMAGE)
        fingerprint = compute_boundary_fingerprint(
            module_id=module_id,
            implementation_entities=list(snapshot.get("implementation_entities") or []),
            test_entities=list(snapshot.get("test_entities") or []),
            interfaces=list(snapshot.get("interfaces") or []),
            readonly_files=list(readonly_files or []),
            test_dir=snapshot.get("test_dir"),
        )
        request = CandidateExecutionRequest(
            candidate_id=cid,
            run_id=run_id,
            node_id=node_id,
            project_root=self.project_root,
            base_map_seq=base_map_seq,
            write_set=tuple(workspace.write_set),
            read_set=tuple(workspace.read_set),
            readonly_files=tuple(workspace.readonly_files),
            tests=tuple(test_commands),
            timeout_seconds=timeout_seconds,
            network_allowed=network_allowed,
            module_id=module_id,
            boundary_fingerprint=fingerprint,
            image_version=image_version,
        )
        request.validate()
        return CandidateSetup(
            candidate_id=cid,
            request=request,
            workspace=workspace,
            boundary_fingerprint=fingerprint,
            module_id=module_id,
            module_root=workspace.module_root,
            map_snapshot=snapshot,
        )

    def prepare(
        self,
        *,
        run_id: str,
        node: dict[str, Any],
        base_map_seq: int,
        readonly_files: list[str],
        interface_rows: list[dict[str, Any]] | None = None,
        candidate_id: str | None = None,
        network_allowed: bool = False,
        timeout_seconds: int = 300,
        map_snapshot: dict[str, Any] | None = None,
    ) -> CandidateSetup:
        if candidate_id is not None:
            validate_safe_id(candidate_id, field="candidate_id")
        if map_snapshot is None:
            raise ValueError("module_execution_snapshot_required")
        return self.prepare_from_snapshot(
            map_snapshot,
            run_id=run_id,
            readonly_files=readonly_files,
            candidate_id=candidate_id,
            network_allowed=network_allowed,
            timeout_seconds=timeout_seconds,
            base_map_seq=base_map_seq,
        )
