"""Candidate execution request/result contracts for container-isolated agent runs."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from bridle.agent.container.candidate_path_guard import candidate_root as resolve_candidate_root
from bridle.agent.container.candidate_path_guard import validate_safe_id
from bridle.agent.tools.proposal_path_validator import ProposalPathValidator

_CANDIDATE_ID_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$")
_MIN_TIMEOUT = 1
_MAX_TIMEOUT = 3600
_TEST_CONTRACT_SCHEMA = "bridle.test_contract/v1"


@dataclass(frozen=True)
class TestFileSnapshot:
    path: str
    sha256: str


@dataclass(frozen=True)
class TestCaseSnapshot:
    case_id: str
    node_id: str


@dataclass(frozen=True)
class TestCommandSnapshot:
    command_id: str
    argv: tuple[str, ...]
    raw_command: str
    test_entity_id: str
    map_seq: int


@dataclass(frozen=True)
class CandidateSubmission:
    """Immutable identity for one frozen candidate tree."""

    submission_id: str
    candidate_id: str
    revision: int
    base_map_seq: int
    boundary_fingerprint: str
    image_version: str
    base_tree_hash: str
    candidate_tree_hash: str
    changed_paths: tuple[str, ...]
    base_hashes: tuple[tuple[str, str], ...]
    candidate_hashes: tuple[tuple[str, str], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "submission_id": self.submission_id,
            "candidate_id": self.candidate_id,
            "revision": self.revision,
            "base_map_seq": self.base_map_seq,
            "boundary_fingerprint": self.boundary_fingerprint,
            "image_version": self.image_version,
            "base_tree_hash": self.base_tree_hash,
            "candidate_tree_hash": self.candidate_tree_hash,
            "changed_paths": list(self.changed_paths),
            "base_hashes": dict(self.base_hashes),
            "candidate_hashes": dict(self.candidate_hashes),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> CandidateSubmission:
        return cls(
            submission_id=str(payload["submission_id"]),
            candidate_id=str(payload["candidate_id"]),
            revision=int(payload["revision"]),
            base_map_seq=int(payload["base_map_seq"]),
            boundary_fingerprint=str(payload["boundary_fingerprint"]),
            image_version=str(payload["image_version"]),
            base_tree_hash=str(payload["base_tree_hash"]),
            candidate_tree_hash=str(payload["candidate_tree_hash"]),
            changed_paths=tuple(str(path) for path in payload.get("changed_paths") or []),
            base_hashes=tuple(
                sorted((str(path), str(digest)) for path, digest in payload["base_hashes"].items())
            ),
            candidate_hashes=tuple(
                sorted(
                    (str(path), str(digest))
                    for path, digest in payload["candidate_hashes"].items()
                )
            ),
        )


@dataclass(frozen=True)
class SubmissionValidation:
    status: str
    error_code: str | None = None


@dataclass(frozen=True)
class FrozenTestContract:
    """Immutable requirements proven by both red and final verification."""

    contract_version: str
    test_files: tuple[TestFileSnapshot, ...]
    cases: tuple[TestCaseSnapshot, ...]
    commands: tuple[TestCommandSnapshot, ...]
    expected_failure_case_ids: tuple[str, ...]
    baseline_hash: str
    map_seq: int
    boundary_fingerprint: str
    image_version: str
    schema: str = _TEST_CONTRACT_SCHEMA

    @classmethod
    def freeze(
        cls,
        *,
        test_files: tuple[TestFileSnapshot, ...],
        cases: tuple[TestCaseSnapshot, ...],
        commands: tuple[TestCommandSnapshot, ...],
        expected_failure_case_ids: tuple[str, ...],
        baseline_hash: str,
        map_seq: int,
        boundary_fingerprint: str,
        image_version: str,
    ) -> FrozenTestContract:
        normalized_files = tuple(sorted(test_files, key=lambda item: item.path))
        normalized_cases = tuple(sorted(cases, key=lambda item: item.node_id))
        normalized_commands = tuple(sorted(commands, key=lambda item: item.command_id))
        normalized_expected = tuple(sorted(set(expected_failure_case_ids)))
        case_ids = {item.case_id for item in normalized_cases}
        if not normalized_files:
            raise ValueError("test_contract_files_required")
        if not normalized_cases:
            raise ValueError("test_contract_cases_required")
        if not normalized_commands:
            raise ValueError("test_contract_commands_required")
        if not normalized_expected:
            raise ValueError("test_contract_expected_failure_scope_required")
        if not set(normalized_expected).issubset(case_ids):
            raise ValueError("test_contract_expected_failure_case_unknown")
        if map_seq < 0:
            raise ValueError("test_contract_map_seq_invalid")
        for field, value in (
            ("baseline_hash", baseline_hash),
            ("boundary_fingerprint", boundary_fingerprint),
            ("image_version", image_version),
        ):
            if not str(value).strip():
                raise ValueError(f"test_contract_{field}_required")
        payload = cls._snapshot_payload(
            test_files=normalized_files,
            cases=normalized_cases,
            commands=normalized_commands,
            expected_failure_case_ids=normalized_expected,
            baseline_hash=baseline_hash,
            map_seq=map_seq,
            boundary_fingerprint=boundary_fingerprint,
            image_version=image_version,
        )
        version = hashlib.sha256(cls._canonical_json(payload).encode("utf-8")).hexdigest()
        return cls(
            contract_version=version,
            test_files=normalized_files,
            cases=normalized_cases,
            commands=normalized_commands,
            expected_failure_case_ids=normalized_expected,
            baseline_hash=str(baseline_hash),
            map_seq=int(map_seq),
            boundary_fingerprint=str(boundary_fingerprint),
            image_version=str(image_version),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            **self._snapshot_payload(
                test_files=self.test_files,
                cases=self.cases,
                commands=self.commands,
                expected_failure_case_ids=self.expected_failure_case_ids,
                baseline_hash=self.baseline_hash,
                map_seq=self.map_seq,
                boundary_fingerprint=self.boundary_fingerprint,
                image_version=self.image_version,
            ),
        }

    def to_json(self) -> str:
        return self._canonical_json(self.to_dict())

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> FrozenTestContract:
        if str(payload.get("schema") or "") != _TEST_CONTRACT_SCHEMA:
            raise ValueError("test_contract_schema_invalid")
        frozen = cls.freeze(
            test_files=tuple(
                TestFileSnapshot(path=str(item["path"]), sha256=str(item["sha256"]))
                for item in payload.get("test_files") or []
            ),
            cases=tuple(
                TestCaseSnapshot(case_id=str(item["case_id"]), node_id=str(item["node_id"]))
                for item in payload.get("cases") or []
            ),
            commands=tuple(
                TestCommandSnapshot(
                    command_id=str(item["command_id"]),
                    argv=tuple(str(arg) for arg in item.get("argv") or []),
                    raw_command=str(item["raw_command"]),
                    test_entity_id=str(item["test_entity_id"]),
                    map_seq=int(item["map_seq"]),
                )
                for item in payload.get("commands") or []
            ),
            expected_failure_case_ids=tuple(
                str(item) for item in payload.get("expected_failure_case_ids") or []
            ),
            baseline_hash=str(payload.get("baseline_hash") or ""),
            map_seq=int(payload.get("map_seq", -1)),
            boundary_fingerprint=str(payload.get("boundary_fingerprint") or ""),
            image_version=str(payload.get("image_version") or ""),
        )
        if frozen.contract_version != str(payload.get("contract_version") or ""):
            raise ValueError("test_contract_version_mismatch")
        return frozen

    def diff(self, current: FrozenTestContract) -> tuple[str, ...]:
        differences: list[str] = []
        for name, frozen_value, current_value in (
            ("test_files", self.test_files, current.test_files),
            ("cases", self.cases, current.cases),
            ("commands", self.commands, current.commands),
            (
                "expected_failure_scope",
                self.expected_failure_case_ids,
                current.expected_failure_case_ids,
            ),
            ("baseline_hash", self.baseline_hash, current.baseline_hash),
            ("map_seq", self.map_seq, current.map_seq),
            (
                "boundary_fingerprint",
                self.boundary_fingerprint,
                current.boundary_fingerprint,
            ),
            ("image_version", self.image_version, current.image_version),
        ):
            if frozen_value != current_value:
                differences.append(name)
        return tuple(differences)

    @staticmethod
    def _canonical_json(payload: dict[str, Any]) -> str:
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _snapshot_payload(
        *,
        test_files: tuple[TestFileSnapshot, ...],
        cases: tuple[TestCaseSnapshot, ...],
        commands: tuple[TestCommandSnapshot, ...],
        expected_failure_case_ids: tuple[str, ...],
        baseline_hash: str,
        map_seq: int,
        boundary_fingerprint: str,
        image_version: str,
    ) -> dict[str, Any]:
        return {
            "schema": _TEST_CONTRACT_SCHEMA,
            "test_files": [asdict(item) for item in test_files],
            "cases": [asdict(item) for item in cases],
            "commands": [
                {
                    **asdict(item),
                    "argv": list(item.argv),
                }
                for item in commands
            ],
            "expected_failure_case_ids": list(expected_failure_case_ids),
            "baseline_hash": str(baseline_hash),
            "map_seq": int(map_seq),
            "boundary_fingerprint": str(boundary_fingerprint),
            "image_version": str(image_version),
        }


@dataclass(frozen=True)
class CandidateExecutionRequest:
    """One isolated candidate run scoped to a map module boundary."""

    candidate_id: str
    run_id: str
    node_id: str
    project_root: Path
    base_map_seq: int
    write_set: tuple[str, ...]
    read_set: tuple[str, ...]
    readonly_files: tuple[str, ...]
    tests: tuple[str, ...]
    timeout_seconds: int
    network_allowed: bool
    module_id: str = ""
    boundary_fingerprint: str = ""
    image_version: str = "local"

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "run_id": self.run_id,
            "node_id": self.node_id,
            "project_root": str(self.project_root.resolve()),
            "base_map_seq": self.base_map_seq,
            "write_set": list(self.write_set),
            "read_set": list(self.read_set),
            "readonly_files": list(self.readonly_files),
            "tests": list(self.tests),
            "timeout_seconds": self.timeout_seconds,
            "network_allowed": self.network_allowed,
            "module_id": self.module_id,
            "boundary_fingerprint": self.boundary_fingerprint,
            "image_version": self.image_version,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> CandidateExecutionRequest:
        request = cls(
            candidate_id=str(payload.get("candidate_id") or ""),
            run_id=str(payload.get("run_id") or ""),
            node_id=str(payload.get("node_id") or ""),
            project_root=Path(str(payload.get("project_root") or ".")).resolve(),
            base_map_seq=int(payload.get("base_map_seq") or 0),
            write_set=tuple(str(item) for item in payload.get("write_set") or []),
            read_set=tuple(str(item) for item in payload.get("read_set") or []),
            readonly_files=tuple(
                str(item) for item in payload.get("readonly_files") or []
            ),
            tests=tuple(str(item) for item in payload.get("tests") or []),
            timeout_seconds=int(payload.get("timeout_seconds") or 0),
            network_allowed=bool(payload.get("network_allowed", False)),
            module_id=str(payload.get("module_id") or ""),
            boundary_fingerprint=str(payload.get("boundary_fingerprint") or ""),
            image_version=str(payload.get("image_version") or "local"),
        )
        request.validate()
        return request

    @property
    def candidate_root(self) -> Path:
        if not self.module_id:
            raise ValueError("module_id_required")
        return resolve_candidate_root(self.project_root, self.module_id, self.candidate_id)

    @property
    def project_dir(self) -> Path:
        return self.candidate_root / "project"

    def validate(self) -> None:
        errors = validate_candidate_request(self)
        if errors:
            raise ValueError("; ".join(errors))


@dataclass(frozen=True)
class CandidateExecutionResult:
    """Structured outcome of one candidate execution turn."""

    status: str
    changed_paths: tuple[str, ...]
    patches: tuple[dict[str, Any], ...]
    base_hashes: dict[str, str]
    candidate_hashes: dict[str, str]
    test_results: tuple[dict[str, Any], ...]
    container: dict[str, Any]
    diagnostic_path: str
    error_code: str | None = None
    candidate_id: str = ""
    base_map_seq: int = 0
    verification: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["patches"] = list(self.patches)
        payload["test_results"] = list(self.test_results)
        payload["changed_paths"] = list(self.changed_paths)
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> CandidateExecutionResult:
        return cls(
            status=str(payload.get("status", "failed")),
            changed_paths=tuple(str(p) for p in payload.get("changed_paths") or []),
            patches=tuple(dict(p) for p in payload.get("patches") or []),
            base_hashes=dict(payload.get("base_hashes") or {}),
            candidate_hashes=dict(payload.get("candidate_hashes") or {}),
            test_results=tuple(dict(r) for r in payload.get("test_results") or []),
            container=dict(payload.get("container") or {}),
            diagnostic_path=str(payload.get("diagnostic_path", "")),
            error_code=payload.get("error_code"),
            candidate_id=str(payload.get("candidate_id", "")),
            base_map_seq=int(payload.get("base_map_seq", 0)),
            verification=dict(payload.get("verification") or {}) if payload.get("verification") else None,
        )


def validate_candidate_request(request: CandidateExecutionRequest) -> list[str]:
    """Return validation errors; empty list means the request is acceptable."""
    errors: list[str] = []
    cid = request.candidate_id.strip()
    if not cid:
        errors.append("candidate_id_required")
    else:
        try:
            validate_safe_id(cid, field="candidate_id")
        except ValueError as exc:
            if hasattr(exc, "error_code"):
                errors.append(str(exc.error_code))
            else:
                errors.append("candidate_id_invalid")
    if not str(request.module_id).strip():
        errors.append("module_id_required")
    else:
        try:
            validate_safe_id(request.module_id, field="module_id")
        except ValueError as exc:
            if hasattr(exc, "error_code"):
                errors.append(str(exc.error_code))
            else:
                errors.append("module_id_invalid")

    project_root = request.project_root.resolve()
    if "module_id_required" not in errors and "module_id_invalid" not in errors:
        try:
            candidate_root = request.candidate_root.resolve()
            candidate_root.relative_to(project_root)
        except ValueError:
            errors.append("candidate_root_outside_project")

    if request.timeout_seconds < _MIN_TIMEOUT or request.timeout_seconds > _MAX_TIMEOUT:
        errors.append("timeout_out_of_range")

    seen_paths: set[str] = set()
    for label, paths in (
        ("write_set", request.write_set),
        ("read_set", request.read_set),
        ("readonly_files", request.readonly_files),
    ):
        for raw in paths:
            path_errors = _validate_relative_path(raw)
            if path_errors:
                errors.extend(f"{label}:{e}" for e in path_errors)
                continue
            norm = ProposalPathValidator.normalize_workspace_relative(str(raw))
            if norm in seen_paths and label == "write_set":
                errors.append(f"duplicate_path:{norm}")
            seen_paths.add(norm)

    if request.base_map_seq < 0:
        errors.append("base_map_seq_invalid")

    return errors


def _validate_relative_path(raw: str) -> list[str]:
    text = str(raw).strip()
    if not text:
        return ["empty_path"]
    if text.startswith("/"):
        return ["absolute_posix_path"]
    if "\\" in text:
        return ["backslash_path"]
    if len(text) >= 2 and text[1] == ":":
        return ["windows_drive_path"]
    normalized = text.replace("\\", "/")
    if ".." in normalized.split("/"):
        return ["parent_traversal"]
    norm = ProposalPathValidator.normalize_workspace_relative(text)
    if not norm:
        return ["empty_after_normalization"]
    return []


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def snapshot_directory_hashes(root: Path, relative_paths: list[str]) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for rel in relative_paths:
        target = root / Path(*rel.split("/"))
        if target.is_file():
            hashes[rel] = file_sha256(target)
    return hashes


def compute_patches(
    *,
    base_hashes: dict[str, str],
    candidate_hashes: dict[str, str],
    write_set: list[str],
) -> tuple[list[str], list[dict[str, Any]]]:
    """Diff baseline vs candidate for declared write set paths."""
    changed: list[str] = []
    patches: list[dict[str, Any]] = []
    all_paths = sorted(set(write_set) | set(base_hashes) | set(candidate_hashes))
    removed = {
        path: base_hashes[path]
        for path in all_paths
        if path in base_hashes and path not in candidate_hashes
    }
    added = {
        path: candidate_hashes[path]
        for path in all_paths
        if path in candidate_hashes and path not in base_hashes
    }
    rename_old_paths: set[str] = set()
    rename_new_paths: set[str] = set()
    rename_patches: list[dict[str, Any]] = []
    for old_path, digest in sorted(removed.items()):
        matches = sorted(
            path
            for path, candidate_digest in added.items()
            if candidate_digest == digest and path not in rename_new_paths
        )
        if not matches:
            continue
        new_path = matches[0]
        rename_old_paths.add(old_path)
        rename_new_paths.add(new_path)
        rename_patches.append(
            {
                "old_path": old_path,
                "path": new_path,
                "change_type": "rename",
                "base_hash": digest,
                "candidate_hash": digest,
            }
        )
    for rel in all_paths:
        base = base_hashes.get(rel)
        cand = candidate_hashes.get(rel)
        if base == cand:
            continue
        changed.append(rel)
        if rel in rename_old_paths or rel in rename_new_paths:
            continue
        if base is None and cand is not None:
            change_type = "add"
        elif base is not None and cand is None:
            change_type = "remove"
        else:
            change_type = "modify"
        patches.append(
            {
                "path": rel,
                "change_type": change_type,
                "base_hash": base,
                "candidate_hash": cand,
            }
        )
    patches.extend(rename_patches)
    patches.sort(key=lambda patch: str(patch["path"]))
    return changed, patches


def persist_result(result: CandidateExecutionResult, candidate_root: Path) -> Path:
    """Write result.json atomically under the candidate directory."""
    candidate_root.mkdir(parents=True, exist_ok=True)
    target = candidate_root / "result.json"
    tmp = candidate_root / "result.json.tmp"
    tmp.write_text(json.dumps(result.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(target)
    return target
