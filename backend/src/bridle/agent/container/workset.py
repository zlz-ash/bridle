"""Map-driven module workset resolution for candidate workspaces."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bridle.agent.tools.proposal_path_validator import ProposalPathValidator


@dataclass(frozen=True)
class MapInterfaceMock:
    interface_id: str
    from_module: str
    to_module: str
    file_path: str
    mock_hash: str
    entity_version: str


@dataclass(frozen=True)
class MapWorksetInput:
    module_id: str
    node_id: str
    implementation_files: tuple[str, ...]
    test_files: tuple[str, ...]
    test_commands: tuple[str, ...]
    interface_mocks: tuple[MapInterfaceMock, ...] = ()
    readonly_context: tuple[str, ...] = ()
    test_dir: str | None = None


@dataclass
class WorksetFileEntry:
    relative_path: str
    source: str
    entity_kind: str
    module_id: str
    interface_id: str | None = None
    mock_hash: str | None = None
    entity_version: str | None = None


@dataclass
class MapWorksetResult:
    write_set: list[str]
    read_set: list[str]
    readonly_files: list[str]
    tests: list[str]
    entries: list[WorksetFileEntry] = field(default_factory=list)
    interfaces: list[dict[str, Any]] = field(default_factory=list)
    error_code: str | None = None
    error_detail: dict[str, Any] = field(default_factory=dict)


class ModuleWorksetResolver:
    """Resolve module-scoped files from authoritative map inputs."""

    def __init__(self, project_root: str | Path) -> None:
        self.project_root = Path(project_root).resolve()

    def resolve(self, workset: MapWorksetInput) -> MapWorksetResult:
        entries: list[WorksetFileEntry] = []
        write_set: set[str] = set()
        read_set: set[str] = set()
        readonly: set[str] = set()

        for rel in workset.implementation_files:
            norm = self._normalize(rel)
            if norm is None:
                return self._error("module_boundary_incomplete", {"path": rel, "reason": "invalid_path"})
            if not self._source_exists(norm):
                return self._error(
                    "module_boundary_incomplete",
                    {"path": norm, "reason": "missing_map_entity_file"},
                )
            write_set.add(norm)
            entries.append(
                WorksetFileEntry(
                    relative_path=norm,
                    source="map_module_file",
                    entity_kind="implementation",
                    module_id=workset.module_id,
                )
            )

        for rel in workset.test_files:
            norm = self._normalize(rel)
            if norm is None:
                return self._error("module_boundary_incomplete", {"path": rel, "reason": "invalid_path"})
            if not self._source_exists(norm):
                return self._error(
                    "module_boundary_incomplete",
                    {"path": norm, "reason": "missing_test_entity_file"},
                )
            write_set.add(norm)
            entries.append(
                WorksetFileEntry(
                    relative_path=norm,
                    source="map_test_entity",
                    entity_kind="test",
                    module_id=workset.module_id,
                )
            )

        interface_records: list[dict[str, Any]] = []
        for mock in workset.interface_mocks:
            norm = self._normalize(mock.file_path)
            if norm is None:
                return self._error(
                    "module_boundary_incomplete",
                    {"path": mock.file_path, "reason": "invalid_mock_path"},
                )
            mock_path = self.project_root / Path(*norm.split("/"))
            if not mock_path.is_file():
                return self._error(
                    "module_boundary_incomplete",
                    {"path": norm, "reason": "missing_interface_mock"},
                )
            actual_hash = hashlib.sha256(mock_path.read_bytes()).hexdigest()
            if mock.mock_hash and mock.mock_hash != actual_hash:
                return self._error(
                    "module_boundary_incomplete",
                    {
                        "path": norm,
                        "reason": "mock_hash_mismatch",
                        "expected": mock.mock_hash,
                        "actual": actual_hash,
                    },
                )
            readonly.add(norm)
            read_set.add(norm)
            entries.append(
                WorksetFileEntry(
                    relative_path=norm,
                    source="map_interface_mock",
                    entity_kind="mock",
                    module_id=workset.module_id,
                    interface_id=mock.interface_id,
                    mock_hash=actual_hash,
                    entity_version=mock.entity_version or actual_hash,
                )
            )
            interface_records.append(
                {
                    "interface_id": mock.interface_id,
                    "from_module": mock.from_module,
                    "to_module": mock.to_module,
                    "file_path": norm,
                    "mock_hash": actual_hash,
                    "entity_version": mock.entity_version or actual_hash,
                }
            )

        for rel in workset.readonly_context:
            norm = self._normalize(rel)
            if norm is None:
                return self._error("module_boundary_incomplete", {"path": rel, "reason": "invalid_readonly_path"})
            if not self._source_exists(norm):
                return self._error(
                    "module_boundary_incomplete",
                    {"path": norm, "reason": "missing_readonly_context"},
                )
            readonly.add(norm)
            read_set.add(norm)
            entries.append(
                WorksetFileEntry(
                    relative_path=norm,
                    source="readonly_context",
                    entity_kind="context",
                    module_id=workset.module_id,
                )
            )

        for rel in sorted(write_set):
            if rel not in readonly:
                read_set.add(rel)

        return MapWorksetResult(
            write_set=sorted(write_set),
            read_set=sorted(read_set),
            readonly_files=sorted(readonly),
            tests=list(workset.test_commands),
            entries=entries,
            interfaces=interface_records,
        )

    def _normalize(self, path: str) -> str | None:
        raw = str(path).strip()
        if not raw or raw.startswith("/") or (len(raw) >= 2 and raw[1] == ":") or "\\" in raw:
            return None
        if ".." in raw.replace("\\", "/").split("/"):
            return None
        norm = ProposalPathValidator.normalize_workspace_relative(raw)
        return norm or None

    def _source_exists(self, norm: str) -> bool:
        path = self.project_root / Path(*norm.split("/"))
        if path.is_symlink():
            return self._safe_symlink_target(path) is not None
        return path.is_file()

    def _safe_symlink_target(self, link: Path) -> Path | None:
        try:
            target = link.resolve()
            target.relative_to(self.project_root)
            return target if target.is_file() else None
        except (OSError, ValueError):
            return None

    @staticmethod
    def _error(code: str, detail: dict[str, Any]) -> MapWorksetResult:
        return MapWorksetResult(
            write_set=[],
            read_set=[],
            readonly_files=[],
            tests=[],
            error_code=code,
            error_detail=detail,
        )
