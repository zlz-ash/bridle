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
    for rel in all_paths:
        base = base_hashes.get(rel)
        cand = candidate_hashes.get(rel)
        if base == cand:
            continue
        changed.append(rel)
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
    return changed, patches


def persist_result(result: CandidateExecutionResult, candidate_root: Path) -> Path:
    """Write result.json atomically under the candidate directory."""
    candidate_root.mkdir(parents=True, exist_ok=True)
    target = candidate_root / "result.json"
    tmp = candidate_root / "result.json.tmp"
    tmp.write_text(json.dumps(result.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(target)
    return target
