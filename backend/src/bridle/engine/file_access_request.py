"""File access request evaluation for sandbox patch boundaries."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from bridle.engine.proposal_path_validator import ProposalPathValidator

LOW_RISK_BASENAMES = frozenset({
    "__init__.py",
    "conftest.py",
    "pyproject.toml",
    "setup.cfg",
    "pytest.ini",
})

HIGH_RISK_MARKERS = (
    ".env",
    "credentials",
    "secret",
    "id_rsa",
    ".github/workflows",
    "docker-compose",
    "alembic",
)


@dataclass(frozen=True)
class FileAccessDecision:
    requested_path: str
    normalized_path: str
    risk_level: str
    auto_approve: bool
    reason: str

    def to_request_payload(
        self,
        *,
        change_type: str,
        reason: str,
        evidence: dict | None,
        node_id: str,
        run_id: str,
        status: str,
    ) -> dict:
        return {
            "requested_path": self.requested_path,
            "normalized_path": self.normalized_path,
            "change_type": change_type,
            "reason": reason,
            "evidence": evidence or {},
            "risk_level": self.risk_level,
            "node_id": node_id,
            "run_id": run_id,
            "status": status,
            "auto_approve": self.auto_approve,
            "decision_reason": self.reason,
        }


def _path_boundary_errors(path: str, workspace_root: Path) -> list[str]:
    errors: list[str] = []
    if not path or not str(path).strip():
        return ["Empty path for patch"]
    raw = str(path).strip()
    if raw.startswith("/"):
        errors.append("Absolute POSIX path is not allowed")
    lowered = raw.replace("\\", "/")
    if lowered.lower().startswith("c:"):
        errors.append("C: drive paths are not allowed")
    if len(lowered) >= 2 and lowered[1] == ":":
        errors.append("Absolute Windows path is not allowed")
    if "\\" in raw:
        errors.append("Backslash paths are not allowed; use POSIX relative paths")
    if ".." in raw.split("/") or ".." in raw.split("\\"):
        errors.append("Parent traversal '..' is not allowed")
    norm = ProposalPathValidator.normalize_workspace_relative(raw)
    if not norm:
        errors.append("Path is empty after normalization")
        return errors
    resolved = workspace_root.joinpath(*norm.split("/")).resolve()
    try:
        resolved.relative_to(workspace_root.resolve())
    except ValueError:
        errors.append(f"Path resolves outside workspace: {norm}")
    return errors


def _high_risk_content(norm: str) -> bool:
    lower = norm.lower()
    return any(marker in lower for marker in HIGH_RISK_MARKERS)


def is_low_risk_optional_path(norm: str) -> bool:
    basename = norm.rsplit("/", 1)[-1]
    if basename in LOW_RISK_BASENAMES:
        return True
    if norm.startswith("tests/fixtures/"):
        return True
    return False


def evaluate_file_access(
    requested_path: str,
    *,
    workspace_root: Path,
    allowed_files: frozenset[str],
) -> FileAccessDecision:
    norm = ProposalPathValidator.normalize_workspace_relative(requested_path)
    boundary_errors = _path_boundary_errors(requested_path, workspace_root)
    if boundary_errors:
        return FileAccessDecision(
            requested_path=requested_path,
            normalized_path=norm or requested_path,
            risk_level="high",
            auto_approve=False,
            reason="; ".join(boundary_errors),
        )
    if norm in allowed_files:
        return FileAccessDecision(
            requested_path=requested_path,
            normalized_path=norm,
            risk_level="low",
            auto_approve=True,
            reason="Path already in allowed_files",
        )
    if _high_risk_content(norm):
        return FileAccessDecision(
            requested_path=requested_path,
            normalized_path=norm,
            risk_level="high",
            auto_approve=False,
            reason="Path matches high-risk file pattern",
        )
    if is_low_risk_optional_path(norm):
        return FileAccessDecision(
            requested_path=requested_path,
            normalized_path=norm,
            risk_level="low",
            auto_approve=True,
            reason="Low-risk optional helper file inside workspace",
        )
    return FileAccessDecision(
        requested_path=requested_path,
        normalized_path=norm,
        risk_level="high",
        auto_approve=False,
        reason="Path is outside allowed_files and not auto-approvable",
    )
