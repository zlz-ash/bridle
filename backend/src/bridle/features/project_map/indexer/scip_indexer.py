"""Structural / SCIP-style symbol occurrences and precise relation edges.

When the SCIP CLI is unavailable this indexer falls back to tree-sitter structural
analysis and still populates ``code_symbols`` / ``code_occurrences`` so moniker joins
can derive ``calls`` / ``inherits`` / ``references`` edges. Emits ``scip_index_degraded``
when falling back.
"""
from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bridle.features.project_map.indexer.treesitter_indexer import (
    TreeSitterIndexer,
    _parser_for,
    _text,
    _unwrap_python_definition,
)
from bridle.features.workspace.overview_service import WorkspaceOverviewService
from bridle.logging.facade import LoggingFacade, get_logging_facade

_PYTHON_SUFFIXES = (".py",)
_TS_SUFFIXES = (".ts", ".tsx")


def _moniker(rel_path: str, qualified_name: str) -> str:
    return f"{rel_path}::{qualified_name}"


@dataclass
class ScipIndexResult:
    """One structural/SCIP pass."""

    symbols: list[dict[str, Any]] = field(default_factory=list)
    occurrences: list[dict[str, Any]] = field(default_factory=list)
    relations: list[dict[str, Any]] = field(default_factory=list)
    used_scip_cli: bool = False
    degraded: bool = False


class ScipIndexer:
    """Build symbol monikers + occurrences; optional SCIP CLI, else tree-sitter structural."""

    def __init__(self, *, facade: LoggingFacade | None = None) -> None:
        self._facade = facade or get_logging_facade()
        self._structural = TreeSitterIndexer(facade=self._facade)

    def index_paths(
        self,
        workspace: Path,
        rel_paths: list[str],
        *,
        file_entities: list[dict],
        nontest_files: set[str],
    ) -> ScipIndexResult:
        """Index a file subset; tries SCIP once per workspace batch, else structural fallback."""
        root = Path(workspace).resolve()
        if self._scip_available():
            try:
                return self._index_with_scip_cli(root, rel_paths, file_entities, nontest_files)
            except Exception as exc:  # noqa: BLE001
                self._facade.warn_event(
                    "scip_index_degraded",
                    "degraded",
                    detail={"error_code": type(exc).__name__, "reason": "scip_cli_failed"},
                )
        else:
            self._facade.warn_event(
                "scip_index_degraded",
                "degraded",
                detail={"reason": "scip_cli_unavailable"},
            )
        return self._index_structural(root, rel_paths, file_entities, nontest_files, degraded=True)

    def _scip_available(self) -> bool:
        return shutil.which("scip-python") is not None

    def _index_with_scip_cli(
        self,
        root: Path,
        rel_paths: list[str],
        file_entities: list[dict],
        nontest_files: set[str],
    ) -> ScipIndexResult:
        """Run scip-python when present; without protobuf parsing always degrade to structural."""
        cli = shutil.which("scip-python")
        if cli is None:
            raise RuntimeError("scip-python not available")
        completed = subprocess.run(
            [cli, "index", str(root)],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(f"scip-python exited {completed.returncode}")
        # Protobuf parsing is not wired yet — keep structural edges but report degraded.
        self._facade.warn_event(
            "scip_index_degraded",
            "degraded",
            detail={"reason": "protobuf_not_implemented"},
        )
        return self._index_structural(root, rel_paths, file_entities, nontest_files, degraded=True)

    def _index_structural(
        self,
        root: Path,
        rel_paths: list[str],
        file_entities: list[dict],
        nontest_files: set[str],
        *,
        degraded: bool,
    ) -> ScipIndexResult:
        symbol_by_moniker: dict[str, dict] = {}
        occurrences: list[dict] = []
        relations: list[dict] = []
        entity_paths = {entity["path"] for entity in file_entities if entity["kind"] in ("file", "test")}

        ts_result = self._structural.run(
            root,
            file_entities=[e for e in file_entities if e["kind"] == "file"],
            parse_paths=rel_paths,
            test_dirs=set(),
        )
        for entity in ts_result.symbol_entities:
            qualified = entity["payload"]["qualified_name"]
            rel = entity["path"].split("::", 1)[0]
            moniker = _moniker(rel, qualified)
            symbol_by_moniker[moniker] = {
                "moniker": moniker,
                "def_entity_id": entity["id"],
                "kind": entity["kind"],
                "display_name": entity["name"],
            }
            occurrences.append(
                {
                    "file_path": rel,
                    "moniker": moniker,
                    "role": "definition",
                    "range": entity["payload"]["range"],
                }
            )

        for rel_path in rel_paths:
            if rel_path not in nontest_files:
                continue
            target = root.joinpath(*rel_path.split("/"))
            if not target.is_file():
                continue
            file_relations = self._extract_calls_and_inherits(
                target, rel_path, symbol_by_moniker, entity_paths
            )
            relations.extend(file_relations)

        return ScipIndexResult(
            symbols=list(symbol_by_moniker.values()),
            occurrences=occurrences,
            relations=relations,
            degraded=degraded,
        )

    def _extract_calls_and_inherits(
        self,
        target: Path,
        rel_path: str,
        symbol_by_moniker: dict[str, dict],
        entity_paths: set[str],
    ) -> list[dict]:
        parser = _parser_for(rel_path)
        if parser is None:
            return []
        try:
            tree = parser.parse(target.read_bytes())
        except OSError:
            return []
        if tree.root_node.has_error:
            return []

        suffix = Path(rel_path).suffix
        if suffix in _PYTHON_SUFFIXES:
            return self._python_relations(tree.root_node, rel_path, symbol_by_moniker)
        if suffix in _TS_SUFFIXES:
            return self._typescript_relations(tree.root_node, rel_path, symbol_by_moniker)
        return []

    def _python_relations(self, root_node, rel_path: str, symbols: dict[str, dict]) -> list[dict]:
        relations: list[dict] = []
        file_id = WorkspaceOverviewService._entity_id("file", rel_path)
        current_fn: str | None = None
        current_class: str | None = None

        def walk(node, fn: str | None, cls: str | None) -> None:
            nonlocal current_fn, current_class
            node_fn, node_cls = fn, cls
            unwrapped = _unwrap_python_definition(node)
            if unwrapped is not None:
                if unwrapped.type == "function_definition":
                    name_node = unwrapped.child_by_field_name("name")
                    if name_node is not None:
                        qn = f"{cls}.{ _text(name_node)}" if cls else _text(name_node)
                        node_fn = qn
                elif unwrapped.type == "class_definition":
                    name_node = unwrapped.child_by_field_name("name")
                    if name_node is not None:
                        cls_name = _text(name_node)
                        node_cls = cls_name
                        node_fn = None
                        bases = unwrapped.child_by_field_name("superclasses")
                        if bases is not None:
                            for base in bases.children:
                                if base.type in ("identifier", "attribute"):
                                    base_name = _text(base).split(".")[-1]
                                    target = symbols.get(_moniker(rel_path, base_name))
                                    if target is None:
                                        for key, sym in symbols.items():
                                            if sym["display_name"] == base_name:
                                                target = sym
                                                break
                                    if target is not None:
                                        relations.append(
                                            {
                                                "source_id": symbols.get(
                                                    _moniker(rel_path, cls_name), {}
                                                ).get("def_entity_id", file_id),
                                                "target_id": target["def_entity_id"],
                                                "kind": "inherits",
                                                "payload": {"base": base_name},
                                            }
                                        )
            if node.type == "call":
                func = node.child_by_field_name("function")
                callee = self._python_callee_name(func)
                if callee:
                    target = self._resolve_callee(rel_path, callee, symbols)
                    if target is not None:
                        caller_id = symbols.get(_moniker(rel_path, node_fn or ""), {}).get(
                            "def_entity_id", file_id
                        )
                        relations.append(
                            {
                                "source_id": caller_id,
                                "target_id": target["def_entity_id"],
                                "kind": "calls",
                                "payload": {"callee": callee},
                            }
                        )
            for child in node.children:
                walk(child, node_fn, node_cls)

        walk(root_node, None, None)
        return relations

    @staticmethod
    def _python_callee_name(func_node) -> str | None:
        if func_node is None:
            return None
        if func_node.type == "identifier":
            return _text(func_node)
        if func_node.type == "attribute":
            return _text(func_node).split(".")[-1]
        return None

    @staticmethod
    def _resolve_callee(rel_path: str, callee: str, symbols: dict[str, dict]) -> dict | None:
        local = symbols.get(_moniker(rel_path, callee))
        if local is not None:
            return local
        for sym in symbols.values():
            if sym["display_name"] == callee:
                return sym
        return None

    def _typescript_relations(self, root_node, rel_path: str, symbols: dict[str, dict]) -> list[dict]:
        relations: list[dict] = []
        file_id = WorkspaceOverviewService._entity_id("file", rel_path)

        def walk(node, current_fn: str | None) -> None:
            fn = current_fn
            if node.type == "function_declaration":
                name_node = node.child_by_field_name("name")
                if name_node is not None:
                    fn = _text(name_node)
            if node.type == "call_expression":
                func = node.child_by_field_name("function")
                callee = self._python_callee_name(func)
                if callee:
                    target = self._resolve_callee(rel_path, callee, symbols)
                    if target is not None:
                        caller_id = symbols.get(_moniker(rel_path, fn or ""), {}).get(
                            "def_entity_id", file_id
                        )
                        relations.append(
                            {
                                "source_id": caller_id,
                                "target_id": target["def_entity_id"],
                                "kind": "calls",
                                "payload": {"callee": callee},
                            }
                        )
            for child in node.children:
                walk(child, fn)

        walk(root_node, None)
        return relations
