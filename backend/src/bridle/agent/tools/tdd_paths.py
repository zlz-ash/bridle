"""TDD path mapping — derive test paths from source paths and vice versa.

Centralizes Bridle's heuristic for which paths count as tests so that the
sandbox tool gate, the prompt template, and ``allowed_files`` expansion all
agree on the same rule.

Recognition rule (combined directory + naming, both must be POSIX-normalized
workspace-relative):

* **Directory**: any path whose first segment is ``tests`` or ``test``, or
  whose path contains a ``/tests/`` or ``/test/`` segment.
* **Naming**: basename starts with ``test_`` or ends with ``_test.py``.

Either condition makes a path a test file.

Derivation rule (used to auto-expand ``allowed_files`` for code_change nodes):

* ``src/foo/bar.py`` → ``tests/foo/test_bar.py``
* ``backend/src/bridle/x/y.py`` → ``backend/tests/x/test_y.py``
* ``a/b/c.py`` (no recognized src prefix) → ``tests/test_c.py``
* Non-Python paths return ``None`` (no test mapping inferred).
"""
from __future__ import annotations

from pathlib import PurePosixPath

_TEST_DIR_NAMES = frozenset({"tests", "test"})
_SRC_PREFIX_HINTS = ("src/", "backend/src/")


def _normalize(path: str) -> str:
    return path.replace("\\", "/").strip().lstrip("./").lstrip("/")


def is_test_path(path: str) -> bool:
    """Return True if ``path`` looks like a test file."""
    if not path:
        return False
    norm = _normalize(path)
    if not norm:
        return False
    parts = PurePosixPath(norm).parts
    if not parts:
        return False
    if any(part in _TEST_DIR_NAMES for part in parts):
        return True
    basename = parts[-1]
    if basename.startswith("test_"):
        return True
    if basename.endswith("_test.py"):
        return True
    return False


def derive_test_path(src_path: str) -> str | None:
    """Best-effort guess of the test path matching ``src_path``.

    Returns ``None`` if ``src_path`` is not a Python source file (we don't
    speculate on test conventions for other languages).
    """
    if not src_path:
        return None
    norm = _normalize(src_path)
    if not norm.endswith(".py"):
        return None
    if is_test_path(norm):
        return norm
    p = PurePosixPath(norm)
    basename = p.name
    test_basename = f"test_{basename}"

    # backend/src/<rest>.py → backend/tests/<rest_without_pkg>/test_<basename>
    if norm.startswith("backend/src/"):
        rest = PurePosixPath(norm[len("backend/src/"):])
        # Drop the top-level package segment so tests don't nest under the package name.
        rest_parts = rest.parts[1:-1] if len(rest.parts) >= 2 else rest.parts[:-1]
        return str(PurePosixPath("backend/tests", *rest_parts, test_basename))

    # src/<rest>.py → tests/<rest_dirs>/test_<basename>
    if norm.startswith("src/"):
        rest = PurePosixPath(norm[len("src/"):])
        rest_parts = rest.parts[:-1]
        return str(PurePosixPath("tests", *rest_parts, test_basename))

    # Fallback: tests/test_<basename>
    return str(PurePosixPath("tests", test_basename))


def expand_allowed_files_for_tdd(allowed_files: list[str]) -> list[str]:
    """Return ``allowed_files`` plus inferred test paths for each source file.

    Preserves order, deduplicates, and skips entries whose derived test path
    would equal an existing entry.
    """
    seen: set[str] = set()
    out: list[str] = []
    for entry in allowed_files:
        norm = _normalize(entry)
        if not norm or norm in seen:
            continue
        seen.add(norm)
        out.append(norm)
    for entry in list(out):
        if is_test_path(entry):
            continue
        derived = derive_test_path(entry)
        if derived and derived not in seen:
            seen.add(derived)
            out.append(derived)
    return out
