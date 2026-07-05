"""Integration tests: business modules use observability facade only."""
from __future__ import annotations

import ast
from pathlib import Path

FORBIDDEN_IMPORTS = {"langfuse"}


def _python_files_under(*roots: str) -> list[Path]:
    base = Path(__file__).resolve().parents[2] / "src" / "bridle"
    files: list[Path] = []
    for root in roots:
        path = base / root
        if path.is_file():
            files.append(path)
        elif path.is_dir():
            files.extend(path.rglob("*.py"))
    return files


class TestBusinessIsolation:
    def test_business_modules_do_not_import_langfuse(self) -> None:
        offenders: list[str] = []
        roots = (
            "services",
            "engine",
            "container_entrypoints",
            "api",
            "app.py",
        )
        for path in _python_files_under(*roots):
            if "observability" in path.parts:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        root = alias.name.split(".")[0]
                        if root in FORBIDDEN_IMPORTS:
                            offenders.append(f"{path}: import {alias.name}")
                elif isinstance(node, ast.ImportFrom) and node.module:
                    root = node.module.split(".")[0]
                    if root in FORBIDDEN_IMPORTS:
                        offenders.append(f"{path}: from {node.module}")
        assert offenders == []
