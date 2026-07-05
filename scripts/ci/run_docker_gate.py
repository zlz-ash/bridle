#!/usr/bin/env python3
"""Wrapper to run backend/tests/agent/container/docker_gate.py as a CLI.

docker_gate.py uses relative imports (``from .docker_evidence import ...``) so
it must be loaded as part of its package. This wrapper loads the
``backend/tests/agent/container`` directory as a synthetic package and invokes
``docker_gate.main`` with the passed argv, keeping the workflow CLI stable.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_DOCKER_TEST_SUPPORT_PKG = "bridle_docker_test_support"


def _load_docker_gate(trusted_root: Path):
    support_dir = trusted_root / "backend" / "tests" / "agent" / "container"
    if _DOCKER_TEST_SUPPORT_PKG not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            _DOCKER_TEST_SUPPORT_PKG,
            support_dir / "__init__.py",
            submodule_search_locations=[str(support_dir)],
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[_DOCKER_TEST_SUPPORT_PKG] = module
        spec.loader.exec_module(module)
    name = f"{_DOCKER_TEST_SUPPORT_PKG}.docker_gate"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, support_dir / "docker_gate.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if not argv:
        print("usage: run_docker_gate.py <trusted_root> <evidence_dir> [--source-digest X --image-digest Y --github-sha Z]", file=sys.stderr)
        return 2
    trusted_root = Path(argv[0]).resolve()
    rest = argv[1:]
    gate = _load_docker_gate(trusted_root)
    return int(gate.main(rest))


if __name__ == "__main__":
    raise SystemExit(main())
