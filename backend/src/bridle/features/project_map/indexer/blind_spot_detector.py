"""Detect static blind spots during structural indexing."""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bridle.features.project_map.indexer.treesitter_indexer import (
    _parser_for,
    _python_import_modules,
    _resolve_python_import,
    _text,
    _ts_import_specifier,
)


@dataclass
class BlindSpotRecord:
    """One open blind spot row ready for map_blind_spots insertion."""

    id: str
    kind: str
    file_path: str
    range: dict[str, Any] | None
    detail: dict[str, Any]
    source: str = "static"
    status: str = "open"


@dataclass
class BlindSpotScanResult:
    """Aggregate blind spots from one indexing pass."""

    spots: list[BlindSpotRecord] = field(default_factory=list)


def _spot_id() -> str:
    return f"blind-{uuid.uuid4().hex}"


def _range_payload(node) -> dict[str, int]:
    return {
        "start_line": node.start_point[0] + 1,
        "end_line": node.end_point[0] + 1,
        "start_byte": node.start_byte,
        "end_byte": node.end_byte,
    }


class BlindSpotDetector:
    """Walk source trees and emit static blind spots for unresolvable or dynamic sites."""

    def scan_file(
        self,
        target: Path,
        rel_path: str,
        *,
        nontest_files: set[str],
    ) -> BlindSpotScanResult:
        """Parse one file; output is unresolved imports and dynamic dispatch sites."""
        parser = _parser_for(rel_path)
        if parser is None:
            return BlindSpotScanResult()
        try:
            tree = parser.parse(target.read_bytes())
        except OSError:
            return BlindSpotScanResult()
        if tree.root_node.has_error:
            return BlindSpotScanResult()

        suffix = Path(rel_path).suffix
        if suffix == ".py":
            return self._scan_python(tree.root_node, rel_path, nontest_files=nontest_files)
        if suffix in (".ts", ".tsx"):
            return self._scan_typescript(tree.root_node, rel_path, nontest_files=nontest_files)
        return BlindSpotScanResult()

    def _scan_python(self, root_node, rel_path: str, *, nontest_files: set[str]) -> BlindSpotScanResult:
        spots: list[BlindSpotRecord] = []
        self._walk_python(root_node, rel_path, nontest_files, spots)
        return BlindSpotScanResult(spots=spots)

    def _walk_python(self, node, rel_path: str, nontest_files: set[str], spots: list[BlindSpotRecord]) -> None:
        if node.type == "import_from_statement" or node.type == "import_statement":
            for module in _python_import_modules(node):
                if module and _resolve_python_import(rel_path, module, nontest_files) is None:
                    spots.append(
                        BlindSpotRecord(
                            id=_spot_id(),
                            kind="unresolved_ref",
                            file_path=rel_path,
                            range=_range_payload(node),
                            detail={"module": module, "candidates": []},
                        )
                    )
        if node.type == "call":
            func = node.child_by_field_name("function")
            if func is not None and func.type == "call":
                inner = func.child_by_field_name("function")
                if inner is not None and _text(inner) == "getattr":
                    spots.append(
                        BlindSpotRecord(
                            id=_spot_id(),
                            kind="dynamic_dispatch",
                            file_path=rel_path,
                            range=_range_payload(node),
                            detail={"pattern": "getattr", "expression": _text(node)[:200]},
                        )
                    )
            elif func is not None and func.type == "attribute" and _text(func).endswith("import_module"):
                spots.append(
                    BlindSpotRecord(
                        id=_spot_id(),
                        kind="dynamic_dispatch",
                        file_path=rel_path,
                        range=_range_payload(node),
                        detail={"pattern": "import_module", "expression": _text(node)[:200]},
                    )
                )
        for child in node.children:
            self._walk_python(child, rel_path, nontest_files, spots)

    def _scan_typescript(self, root_node, rel_path: str, *, nontest_files: set[str]) -> BlindSpotScanResult:
        spots: list[BlindSpotRecord] = []
        self._walk_typescript(root_node, rel_path, nontest_files, spots)
        return BlindSpotScanResult(spots=spots)

    def _walk_typescript(self, node, rel_path: str, nontest_files: set[str], spots: list[BlindSpotRecord]) -> None:
        if node.type == "import_statement":
            specifier = _ts_import_specifier(node)
            if specifier and not (specifier.startswith(".") or specifier.startswith("/")):
                spots.append(
                    BlindSpotRecord(
                        id=_spot_id(),
                        kind="unresolved_ref",
                        file_path=rel_path,
                        range=_range_payload(node),
                        detail={"module": specifier, "candidates": []},
                    )
                )
        for child in node.children:
            self._walk_typescript(child, rel_path, nontest_files, spots)

    @staticmethod
    def to_row(spot: BlindSpotRecord) -> dict[str, Any]:
        """Serialize one blind spot for SQLite insertion."""
        return {
            "id": spot.id,
            "kind": spot.kind,
            "file_path": spot.file_path,
            "range": json.dumps(spot.range or {}, ensure_ascii=False),
            "detail": json.dumps(spot.detail, ensure_ascii=False),
            "source": spot.source,
            "status": spot.status,
        }
