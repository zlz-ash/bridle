"""Prepare candidate workspaces from authoritative map execution snapshots."""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bridle.agent.container.boundary import compute_boundary_fingerprint
from bridle.agent.container.candidate_contract import (
    CandidateExecutionRequest,
    CandidateSubmission,
    SubmissionValidation,
    compute_patches,
)
from bridle.agent.container.candidate_path_guard import validate_safe_id
from bridle.agent.container.image_identity import resolve_image_identity
from bridle.agent.container.workset import MapInterfaceMock, MapWorksetInput
from bridle.agent.container.workspace import ContainerWorkspaceBuilder, ContainerWorkspaceResult
from bridle.logging.facade import LoggingFacade, get_logging_facade


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

    def __init__(
        self,
        project_root: str | Path,
        *,
        facade: LoggingFacade | None = None,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self._facade = facade or get_logging_facade()

    def freeze_submission(self, setup: CandidateSetup) -> CandidateSubmission:
        """Freeze current baseline and candidate trees under a monotonic revision."""
        base_hashes = self._tree_hashes(setup.workspace.baseline_dir)
        candidate_hashes = self._tree_hashes(setup.workspace.project_dir)
        changed_paths, _ = compute_patches(
            base_hashes=base_hashes,
            candidate_hashes=candidate_hashes,
            write_set=list(setup.request.write_set),
        )
        directory = self._submission_directory(setup)
        directory.mkdir(parents=True, exist_ok=True)
        revision = self._next_revision(directory)
        base_tree_hash = self._tree_hash(base_hashes)
        candidate_tree_hash = self._tree_hash(candidate_hashes)
        identity = {
            "candidate_id": setup.candidate_id,
            "revision": revision,
            "base_tree_hash": base_tree_hash,
            "candidate_tree_hash": candidate_tree_hash,
        }
        identity_hash = hashlib.sha256(self._canonical_json(identity).encode("utf-8")).hexdigest()
        submission = CandidateSubmission(
            submission_id=f"submission-{revision}-{identity_hash[:16]}",
            candidate_id=setup.candidate_id,
            revision=revision,
            base_map_seq=setup.request.base_map_seq,
            boundary_fingerprint=setup.boundary_fingerprint,
            image_version=setup.request.image_version,
            base_tree_hash=base_tree_hash,
            candidate_tree_hash=candidate_tree_hash,
            changed_paths=tuple(changed_paths),
            base_hashes=tuple(sorted(base_hashes.items())),
            candidate_hashes=tuple(sorted(candidate_hashes.items())),
        )
        target = directory / f"{revision:08d}-{submission.submission_id}.json"
        temporary = target.with_name(f".{target.name}.tmp-{uuid.uuid4().hex}")
        try:
            temporary.write_text(
                json.dumps(submission.to_dict(), ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        self._log_submission("candidate_submission.freeze", "completed", setup, submission)
        return submission

    def validate_submission(
        self,
        setup: CandidateSetup,
        submission: CandidateSubmission,
    ) -> SubmissionValidation:
        """Recompute immutable identities before verification or publication."""
        if (
            submission.candidate_id != setup.candidate_id
            or submission.base_map_seq != setup.request.base_map_seq
            or submission.boundary_fingerprint != setup.boundary_fingerprint
            or submission.image_version != setup.request.image_version
        ):
            result = SubmissionValidation("invalid", "candidate_submission_identity_changed")
        elif self._tree_hash(self._tree_hashes(setup.workspace.baseline_dir)) != submission.base_tree_hash:
            result = SubmissionValidation("invalid", "candidate_baseline_changed")
        elif (
            self._tree_hash(self._tree_hashes(setup.workspace.project_dir))
            != submission.candidate_tree_hash
        ):
            result = SubmissionValidation("invalid", "candidate_submission_changed")
        else:
            result = SubmissionValidation("valid")
        self._log_submission(
            "candidate_submission.validate",
            "completed" if result.status == "valid" else "failed",
            setup,
            submission,
            error_code=result.error_code,
        )
        return result

    def load_submission(self, setup: CandidateSetup, submission_id: str) -> CandidateSubmission:
        validate_safe_id(submission_id, field="submission_id")
        for path in sorted(self._submission_directory(setup).glob("*.json")):
            payload = json.loads(path.read_text(encoding="utf-8"))
            if str(payload.get("submission_id")) != submission_id:
                continue
            submission = CandidateSubmission.from_dict(payload)
            self._log_submission("candidate_submission.load", "completed", setup, submission)
            return submission
        self._facade.info_event(
            "candidate_submission.load",
            "failed",
            trace_id=setup.request.run_id,
            project_id=str(self.project_root),
            error_code="candidate_submission_not_found",
            detail={"candidate_id": setup.candidate_id, "submission_id": submission_id},
        )
        raise FileNotFoundError("candidate_submission_not_found")

    @staticmethod
    def _tree_hashes(root: Path) -> dict[str, str]:
        if not root.is_dir():
            return {}
        return {
            path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(root.rglob("*"))
            if path.is_file()
        }

    @classmethod
    def _tree_hash(cls, hashes: dict[str, str]) -> str:
        return hashlib.sha256(cls._canonical_json(hashes).encode("utf-8")).hexdigest()

    @staticmethod
    def _canonical_json(payload: dict[str, Any]) -> str:
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _next_revision(directory: Path) -> int:
        revisions: list[int] = []
        for path in directory.glob("*.json"):
            try:
                revisions.append(int(path.name.split("-", 1)[0]))
            except ValueError:
                continue
        return max(revisions, default=0) + 1

    def _submission_directory(self, setup: CandidateSetup) -> Path:
        return self.project_root / ".bridle" / "candidate-submissions" / setup.candidate_id

    def _log_submission(
        self,
        action: str,
        status: str,
        setup: CandidateSetup,
        submission: CandidateSubmission,
        *,
        error_code: str | None = None,
    ) -> None:
        self._facade.info_event(
            action,
            status,
            trace_id=setup.request.run_id,
            project_id=str(self.project_root),
            error_code=error_code,
            detail={
                "candidate_id": submission.candidate_id,
                "submission_id": submission.submission_id,
                "revision": submission.revision,
                "changed_path_count": len(submission.changed_paths),
            },
        )

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
        workspace_builder = ContainerWorkspaceBuilder(self.project_root)
        workspace = workspace_builder.build_candidate_workspace(
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
        workspace_builder.persist_candidate_request(workspace, request)
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
