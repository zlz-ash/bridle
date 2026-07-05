"""tree-sitter structural indexer.

Parses ``.py`` / ``.ts`` / ``.tsx`` source into symbol entities (function / class / method)
and literal ``imports`` edges, and classifies test-folder files. This is the always-on
structural floor of the code map: it must never crash on bad input — a file that fails to parse
degrades to a plain ``file`` entity and emits ``treesitter_parse_degraded``.
"""
from __future__ import annotations

import posixpath
from collections.abc import Iterable
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any

from bridle.features.workspace.overview_service import WorkspaceOverviewService
from bridle.logging.facade import LoggingFacade, get_logging_facade

_PYTHON_SUFFIXES = (".py",)
_TS_SUFFIXES = (".ts", ".tsx")
_SOURCE_SUFFIXES = _PYTHON_SUFFIXES + _TS_SUFFIXES
_TS_RESOLVE_SUFFIXES = (".ts", ".tsx", ".d.ts", ".js", ".jsx")


@dataclass(frozen=True)
class IndexResult:
    """One tree-sitter pass; fields are symbol entities, imports edges, test paths, degraded paths."""

    symbol_entities: list[dict] = field(default_factory=list)
    relations: list[dict] = field(default_factory=list)
    test_paths: set[str] = field(default_factory=set)
    degraded_paths: list[str] = field(default_factory=list)


def _test_dir_parent(declared: str) -> str:
    """Return the module a declared test_dir belongs to; 'pkg/spec' -> 'pkg', 'spec' -> ''."""
    normalized = declared.strip("/")
    return normalized.rsplit("/", 1)[0] if "/" in normalized else ""


def classify_is_test(rel_path: str, test_dirs: Iterable[str]) -> bool:
    """Decide if a workspace file is a test file; explicit declarations override the default tests/."""
    declared = {entry.strip("/").replace("\\", "/") for entry in test_dirs if entry and entry.strip("/")}
    for directory in declared:
        if rel_path == directory or rel_path.startswith(directory + "/"):
            return True
    suppressed_modules = {_test_dir_parent(directory) for directory in declared}
    parts = rel_path.split("/")
    for index in range(len(parts) - 1):
        if parts[index] == "tests":
            module_prefix = "/".join(parts[:index])
            if module_prefix in suppressed_modules:
                continue
            return True
    return False


@cache
def _python_language():
    """Load the cached tree-sitter Python language object."""
    import tree_sitter_python as ts_python
    from tree_sitter import Language

    return Language(ts_python.language())


@cache
def _typescript_language(is_tsx: bool):
    """Load the cached tree-sitter TypeScript/TSX language object."""
    import tree_sitter_typescript as ts_typescript
    from tree_sitter import Language

    raw = ts_typescript.language_tsx() if is_tsx else ts_typescript.language_typescript()
    return Language(raw)


def _parser_for(rel_path: str):
    """Return a tree-sitter parser for the file's language, or None when unsupported."""
    from tree_sitter import Parser

    suffix = Path(rel_path).suffix
    if suffix in _PYTHON_SUFFIXES:
        return Parser(_python_language())
    if suffix in _TS_SUFFIXES:
        return Parser(_typescript_language(suffix == ".tsx"))
    return None


def _text(node) -> str:
    """Decode one node's source slice as text."""
    return node.text.decode("utf-8", errors="replace")


def _unwrap_python_definition(node):
    """Unwrap a decorated_definition to the underlying function/class node."""
    if node.type == "decorated_definition":
        return node.child_by_field_name("definition")
    return node


def _range_payload(node, language: str, qualified_name: str) -> dict[str, Any]:
    """Build the deterministic payload for one symbol entity."""
    return {
        "qualified_name": qualified_name,
        "language": language,
        "range": {
            "start_line": node.start_point[0] + 1,
            "end_line": node.end_point[0] + 1,
            "start_byte": node.start_byte,
            "end_byte": node.end_byte,
        },
    }


class TreeSitterIndexer:
    """Parse source into symbols + import edges; constructor binds logging, methods return IndexResult."""

    def __init__(self, *, facade: LoggingFacade | None = None) -> None:
        """Bind a logging facade; no parsing happens until index_workspace/run is called."""
        self._facade = facade or get_logging_facade()

    def index_workspace(self, workspace: Path, *, test_dirs: Iterable[str] = ()) -> IndexResult:
        """Full pass over a workspace; input is its root, output is every symbol/edge/test/degrade."""
        base_entities = WorkspaceOverviewService.scan_entities(workspace)
        file_entities = [entity for entity in base_entities if entity["kind"] == "file"]
        return self.run(workspace, file_entities=file_entities, parse_paths=None, test_dirs=test_dirs)

    def run(
        self,
        workspace: Path,
        *,
        file_entities: list[dict],
        parse_paths: list[str] | None,
        test_dirs: Iterable[str] = (),
    ) -> IndexResult:
        """Index a chosen subset; file_entities give the full file set for resolution, parse_paths the
        files to (re)parse (None = all), output is the combined symbols/edges/test-paths/degraded."""
        declared = set(test_dirs)
        root = Path(workspace).resolve()
        all_file_paths = {entity["path"] for entity in file_entities}
        nontest_files = {path for path in all_file_paths if not classify_is_test(path, declared)}
        process_paths = sorted(all_file_paths) if parse_paths is None else sorted(set(parse_paths))

        symbol_entities: list[dict] = []
        relations: list[tuple[str, str, str]] = []
        relation_payloads: dict[tuple[str, str, str], dict] = {}
        test_paths: set[str] = set()
        degraded_paths: list[str] = []

        for rel_path in process_paths:
            if rel_path not in all_file_paths:
                continue
            if classify_is_test(rel_path, declared):
                test_paths.add(rel_path)
                continue
            if Path(rel_path).suffix not in _SOURCE_SUFFIXES:
                continue
            target = root.joinpath(*rel_path.split("/"))
            if not target.is_file():
                continue
            file_symbols, file_relations, degraded = self._index_file(
                target, rel_path, nontest_files=nontest_files
            )
            if degraded:
                degraded_paths.append(rel_path)
                continue
            symbol_entities.extend(file_symbols)
            for relation in file_relations:
                key = (relation["source_id"], relation["target_id"], relation["kind"])
                if key not in relation_payloads:
                    relations.append(key)
                    relation_payloads[key] = relation.get("payload", {})

        return IndexResult(
            symbol_entities=symbol_entities,
            relations=[
                {"source_id": s, "target_id": t, "kind": k, "payload": relation_payloads[(s, t, k)]}
                for (s, t, k) in relations
            ],
            test_paths=test_paths,
            degraded_paths=degraded_paths,
        )

    def _index_file(
        self, target: Path, rel_path: str, *, nontest_files: set[str]
    ) -> tuple[list[dict], list[dict], bool]:
        """Parse one source file; output is (symbols, imports, degraded?) — degraded skips both lists."""
        parser = _parser_for(rel_path)
        if parser is None:
            return [], [], False
        try:
            source = target.read_bytes()
            tree = parser.parse(source)
        except Exception as exc:  # noqa: BLE001 — parsing must never crash the whole scan
            self._facade.warn_event(
                "treesitter_parse_degraded",
                "error",
                detail={"path": rel_path, "error_code": type(exc).__name__},
            )
            return [], [], True

        if tree.root_node.has_error:
            self._facade.warn_event(
                "treesitter_parse_degraded",
                "degraded",
                detail={"path": rel_path, "reason": "syntax_error"},
            )
            return [], [], True

        suffix = Path(rel_path).suffix
        if suffix in _PYTHON_SUFFIXES:
            return self._index_python(tree.root_node, rel_path, nontest_files) + (False,)
        return self._index_typescript(tree.root_node, rel_path, nontest_files) + (False,)

    # --- Python -----------------------------------------------------------------

    def _index_python(
        self, root_node, rel_path: str, nontest_files: set[str]
    ) -> tuple[list[dict], list[dict]]:
        """Walk python top-level + class bodies; output is its symbol entities and import edges."""
        file_id = WorkspaceOverviewService._entity_id("file", rel_path)
        symbols: list[dict] = []
        relations: list[dict] = []

        for child in root_node.children:
            node = _unwrap_python_definition(child)
            if node is None:
                continue
            if node.type == "function_definition":
                self._append_symbol(symbols, rel_path, node, "function", node, file_id, "python")
            elif node.type == "class_definition":
                class_entity = self._append_symbol(
                    symbols, rel_path, node, "class", node, file_id, "python"
                )
                if class_entity is None:
                    continue
                body = node.child_by_field_name("body")
                if body is None:
                    continue
                for member in body.children:
                    member_node = _unwrap_python_definition(member)
                    if member_node is not None and member_node.type == "function_definition":
                        self._append_symbol(
                            symbols,
                            rel_path,
                            member_node,
                            "method",
                            member_node,
                            class_entity["id"],
                            "python",
                            parent_qualifier=class_entity["name"],
                        )
            elif node.type in ("import_statement", "import_from_statement"):
                for module in _python_import_modules(node):
                    resolved = _resolve_python_import(rel_path, module, nontest_files)
                    if resolved is not None:
                        relations.append(self._imports_edge(file_id, resolved, module))
        return symbols, relations

    # --- TypeScript -------------------------------------------------------------

    def _index_typescript(
        self, root_node, rel_path: str, nontest_files: set[str]
    ) -> tuple[list[dict], list[dict]]:
        """Walk TS/TSX top-level (through export wrappers); output is symbol entities and import edges."""
        file_id = WorkspaceOverviewService._entity_id("file", rel_path)
        symbols: list[dict] = []
        relations: list[dict] = []

        for child in root_node.children:
            node = child
            if node.type == "export_statement":
                node = _ts_exported_declaration(node) or node
            if node.type == "function_declaration":
                self._append_symbol(symbols, rel_path, node, "function", node, file_id, "typescript")
            elif node.type in ("class_declaration", "abstract_class_declaration"):
                class_entity = self._append_symbol(
                    symbols, rel_path, node, "class", node, file_id, "typescript"
                )
                if class_entity is None:
                    continue
                body = node.child_by_field_name("body")
                if body is None:
                    continue
                for member in body.children:
                    if member.type == "method_definition":
                        self._append_symbol(
                            symbols,
                            rel_path,
                            member,
                            "method",
                            member,
                            class_entity["id"],
                            "typescript",
                            parent_qualifier=class_entity["name"],
                        )
            elif node.type in ("lexical_declaration", "variable_declaration"):
                for declarator in node.children:
                    if declarator.type != "variable_declarator":
                        continue
                    value = declarator.child_by_field_name("value")
                    if value is not None and value.type in ("arrow_function", "function_expression", "function"):
                        self._append_symbol(
                            symbols, rel_path, declarator, "function", declarator, file_id, "typescript"
                        )
            elif node.type == "import_statement":
                specifier = _ts_import_specifier(node)
                if specifier is not None:
                    resolved = _resolve_ts_import(rel_path, specifier, nontest_files)
                    if resolved is not None:
                        relations.append(self._imports_edge(file_id, resolved, specifier))
        return symbols, relations

    # --- shared helpers ---------------------------------------------------------

    def _append_symbol(
        self,
        symbols: list[dict],
        rel_path: str,
        name_node,
        kind: str,
        range_node,
        parent_id: str,
        language: str,
        *,
        parent_qualifier: str | None = None,
    ) -> dict | None:
        """Build one symbol entity from a named node and append it; return it (or None if unnamed)."""
        identifier = name_node.child_by_field_name("name")
        if identifier is None:
            return None
        name = _text(identifier)
        qualified_name = f"{parent_qualifier}.{name}" if parent_qualifier else name
        entity = {
            "id": WorkspaceOverviewService._entity_id(kind, rel_path, symbol=qualified_name),
            "path": f"{rel_path}::{qualified_name}",
            "kind": kind,
            "name": name,
            "parent_id": parent_id,
            "payload": _range_payload(range_node, language, qualified_name),
        }
        symbols.append(entity)
        return entity

    @staticmethod
    def _imports_edge(source_id: str, target_rel: str, module: str) -> dict:
        """Build one imports edge from a file id to a resolved target file path."""
        return {
            "source_id": source_id,
            "target_id": WorkspaceOverviewService._entity_id("file", target_rel),
            "kind": "imports",
            "payload": {"module": module, "target_path": target_rel},
        }


# --- module-level import parsing / resolution -----------------------------------


def _python_import_modules(node) -> list[str]:
    """Extract module strings from one python import node (dotted or relative text)."""
    modules: list[str] = []
    if node.type == "import_from_statement":
        module_node = None
        for child in node.children:
            if child.type == "import":
                break
            if child.type in ("dotted_name", "relative_import"):
                module_node = child
        if module_node is not None:
            modules.append(_text(module_node))
    elif node.type == "import_statement":
        for child in node.children:
            if child.type == "dotted_name":
                modules.append(_text(child))
            elif child.type == "aliased_import":
                for grandchild in child.children:
                    if grandchild.type == "dotted_name":
                        modules.append(_text(grandchild))
                        break
    return modules


def _resolve_python_import(importing_rel: str, module: str, files: set[str]) -> str | None:
    """Resolve a python module string to an in-workspace non-test file path, or None."""
    if not module:
        return None
    candidates: list[str] = []
    if module.startswith("."):
        leading = len(module) - len(module.lstrip("."))
        base = posixpath.dirname(importing_rel)
        for _ in range(leading - 1):
            base = posixpath.dirname(base)
        remainder = module[leading:]
        parts = [part for part in remainder.split(".") if part]
        target = posixpath.join(base, *parts) if parts else base
        candidates = [f"{target}.py", posixpath.join(target, "__init__.py")]
    else:
        parts = module.split(".")
        root_based = "/".join(parts)
        candidates.append(f"{root_based}.py")
        candidates.append(posixpath.join(root_based, "__init__.py"))
        package_dir = posixpath.dirname(importing_rel)
        nested = posixpath.join(package_dir, *parts) if package_dir else root_based
        candidates.append(f"{nested}.py")
        candidates.append(posixpath.join(nested, "__init__.py"))
    for candidate in candidates:
        normalized = posixpath.normpath(candidate).lstrip("./")
        if normalized in files:
            return normalized
    return None


def _ts_exported_declaration(export_node):
    """Return the declaration wrapped by an export_statement, if any."""
    for child in export_node.children:
        if child.type in (
            "function_declaration",
            "class_declaration",
            "abstract_class_declaration",
            "lexical_declaration",
            "variable_declaration",
        ):
            return child
    return None


def _ts_import_specifier(node) -> str | None:
    """Extract the string specifier from a TS import_statement (e.g. './b')."""
    for child in node.children:
        if child.type == "string":
            for grandchild in child.children:
                if grandchild.type == "string_fragment":
                    return _text(grandchild)
    return None


def _resolve_ts_import(importing_rel: str, specifier: str, files: set[str]) -> str | None:
    """Resolve a relative TS import specifier to an in-workspace non-test file path, or None."""
    if not (specifier.startswith("./") or specifier.startswith("../") or specifier.startswith("/")):
        return None  # bare/external module — recorded as a blind spot in phase two, not here
    base = posixpath.dirname(importing_rel)
    target = posixpath.normpath(posixpath.join(base, specifier)).lstrip("./")
    if target in files:
        return target
    for suffix in _TS_RESOLVE_SUFFIXES:
        candidate = f"{target}{suffix}"
        if candidate in files:
            return candidate
    for suffix in (".ts", ".tsx", ".js", ".jsx"):
        candidate = posixpath.join(target, f"index{suffix}")
        if candidate in files:
            return candidate
    return None
