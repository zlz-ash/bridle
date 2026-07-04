"""SandboxPolicy - per-run permission boundaries."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from bridle.agent.tools.proposal_path_validator import ProposalPathValidator
from bridle.agent.tools.test_command_policy import TestCommandPolicy

MAX_COMMAND_TIMEOUT_SECONDS = 300
DEFAULT_COMMAND_TIMEOUT_SECONDS = 60

_DRIVE_PATTERN = re.compile(r"(?i)^[a-z]:")
_C_DRIVE_PATTERN = re.compile(r"(?i)^[c]:[/\\]")


@dataclass(frozen=True)
class SandboxPolicy:
    """Immutable policy for one agent run."""

    run_id: str
    node_id: str
    workspace_root: Path
    allowed_files: frozenset[str]
    allowed_test_commands: frozenset[str]
    network_allowed: bool = False
    dependency_install_allowed: bool = False
    env_visible: bool = False
    command_timeout_seconds: int = DEFAULT_COMMAND_TIMEOUT_SECONDS
    readonly_files: frozenset[str] = frozenset()

    @classmethod
    def for_run(
        cls,
        *,
        run_id: str,
        node_id: str,
        workspace_root: str | Path,
        allowed_files: list[str],
        node_tests: list[str],
        command_timeout_seconds: int = DEFAULT_COMMAND_TIMEOUT_SECONDS,
        network_allowed: bool = False,
    ) -> SandboxPolicy:
        root = Path(workspace_root).resolve()
        norm_allowed: set[str] = set()
        for raw in allowed_files:
            key = ProposalPathValidator.normalize_workspace_relative(str(raw))
            if key:
                norm_allowed.add(key)

        allowed_cmds: set[str] = set()
        for cmd in node_tests:
            text = str(cmd).strip()
            if text and not TestCommandPolicy.validate(text):
                allowed_cmds.add(text)

        timeout = max(1, int(command_timeout_seconds))
        if timeout > MAX_COMMAND_TIMEOUT_SECONDS:
            raise ValueError(f"command_timeout_seconds exceeds max {MAX_COMMAND_TIMEOUT_SECONDS}")

        return cls(
            run_id=run_id,
            node_id=node_id,
            workspace_root=root,
            allowed_files=frozenset(norm_allowed),
            allowed_test_commands=frozenset(allowed_cmds),
            command_timeout_seconds=timeout,
            network_allowed=network_allowed,
        )

    def with_readonly_files(self, paths: frozenset[str] | set[str]) -> SandboxPolicy:
        readonly: set[str] = set()
        for raw in paths:
            norm = ProposalPathValidator.normalize_workspace_relative(str(raw))
            if norm:
                readonly.add(norm)
        return SandboxPolicy(
            run_id=self.run_id,
            node_id=self.node_id,
            workspace_root=self.workspace_root,
            allowed_files=self.allowed_files,
            allowed_test_commands=self.allowed_test_commands,
            network_allowed=self.network_allowed,
            dependency_install_allowed=self.dependency_install_allowed,
            env_visible=self.env_visible,
            command_timeout_seconds=self.command_timeout_seconds,
            readonly_files=frozenset(readonly),
        )

    def validate_timeout_config(self) -> list[str]:
        if self.command_timeout_seconds > MAX_COMMAND_TIMEOUT_SECONDS:
            return [f"command_timeout_seconds exceeds max {MAX_COMMAND_TIMEOUT_SECONDS}"]
        return []

    def validate_read_path(self, path: str) -> list[str]:
        return self._validate_allowed_path(path, purpose="read")

    def validate_patch_path(self, path: str) -> list[str]:
        """Validate generic patch output; path input exits denied for the PlanService-owned DB."""
        errors = list(self.validate_readonly_patch(path))
        errors.extend(self._validate_allowed_path(path, purpose="patch"))
        normalized = ProposalPathValidator.normalize_workspace_relative(str(path).strip())
        if normalized == ".bridle/plan.db":
            errors.append(".bridle/plan.db can only be changed through PlanService.patch_current")
        return errors

    def validate_test_command(self, command: str) -> list[str]:
        cmd = str(command).strip()
        if not cmd:
            return ["Empty test command"]
        errors = list(TestCommandPolicy.validate(cmd))
        if cmd not in self.allowed_test_commands:
            errors.append("Command is not in node.tests allowlist for this run")
        if not self.network_allowed:
            lowered = cmd.lower()
            if any(x in lowered for x in ("curl", "wget", "http://", "https://")):
                errors.append("Network access is disabled in sandbox policy")
        if not self.dependency_install_allowed:
            lowered = cmd.lower()
            if any(x in lowered for x in ("npm install", "pip install", "uv add")):
                errors.append("Dependency install is disabled in sandbox policy")
        return errors

    def resolve_read_path(self, path: str) -> Path | None:
        errors = self.validate_read_path(path)
        if errors:
            return None
        norm = ProposalPathValidator.normalize_workspace_relative(path)
        parts = norm.split("/")
        return self.workspace_root.joinpath(*parts)

    def with_granted_files(self, extra: frozenset[str] | set[str]) -> SandboxPolicy:
        merged: set[str] = set(self.allowed_files)
        for raw in extra:
            norm = ProposalPathValidator.normalize_workspace_relative(str(raw))
            if norm:
                merged.add(norm)
        return SandboxPolicy(
            run_id=self.run_id,
            node_id=self.node_id,
            workspace_root=self.workspace_root,
            allowed_files=frozenset(merged),
            allowed_test_commands=self.allowed_test_commands,
            network_allowed=self.network_allowed,
            dependency_install_allowed=self.dependency_install_allowed,
            env_visible=self.env_visible,
            command_timeout_seconds=self.command_timeout_seconds,
            readonly_files=self.readonly_files,
        )

    def snapshot(self) -> dict:
        return {
            "run_id": self.run_id,
            "node_id": self.node_id,
            "workspace_root": str(self.workspace_root),
            "allowed_files": sorted(self.allowed_files),
            "allowed_test_commands": sorted(self.allowed_test_commands),
            "network_allowed": self.network_allowed,
            "dependency_install_allowed": self.dependency_install_allowed,
            "env_visible": self.env_visible,
            "command_timeout_seconds": self.command_timeout_seconds,
        }

    def _validate_allowed_path(self, path: str, *, purpose: str) -> list[str]:
        errors: list[str] = []
        if not path or not str(path).strip():
            return [f"Empty path for {purpose}"]

        raw = str(path).strip()
        if raw.startswith("/"):
            errors.append("Absolute POSIX path is not allowed")
        if _DRIVE_PATTERN.match(raw.replace("\\", "/")):
            if _C_DRIVE_PATTERN.match(raw.replace("\\", "/")):
                errors.append("C: drive paths are not allowed")
            else:
                errors.append("Absolute Windows path is not allowed")
        if "\\" in raw:
            errors.append("Backslash paths are not allowed; use POSIX relative paths")
        if ".." in raw.split("/") or ".." in raw.split("\\"):
            errors.append("Parent traversal '..' is not allowed")

        norm = ProposalPathValidator.normalize_workspace_relative(raw)
        if not norm:
            errors.append("Path is empty after normalization")
            return errors

        parts = norm.split("/")
        resolved = self.workspace_root.joinpath(*parts).resolve()
        try:
            resolved.relative_to(self.workspace_root.resolve())
        except ValueError:
            errors.append(f"Path resolves outside workspace: {norm}")

        if norm not in self.allowed_files:
            errors.append(f"Path '{norm}' is not in allowed_files")
        return errors

    def validate_readonly_patch(self, path: str) -> list[str]:
        """Reject patches to readonly mock/interface paths."""
        norm = ProposalPathValidator.normalize_workspace_relative(str(path).strip())
        if norm in self.readonly_files:
            errors = [f"Path '{norm}' is readonly (module interface mock)"]
            return errors
        return []

