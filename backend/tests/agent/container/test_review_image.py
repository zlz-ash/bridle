"""Tests for review image source binding."""
from __future__ import annotations

import hashlib
import shutil
import stat
from contextlib import contextmanager
from pathlib import Path

import pytest

from bridle.agent.container.review_image import (
    PRODUCER_VERSION,
    REVIEW_METADATA_SCHEMA,
    ReviewImageError,
    compute_agent_source_digest,
    find_repo_root,
    iter_agent_source_paths,
    verify_review_image,
)


@pytest.fixture
def repo_root() -> Path:
    return find_repo_root()


@pytest.fixture
def isolated_repo_copy(tmp_path: Path, repo_root: Path) -> Path:
    """Minimal repo tree on D: drive temp for digest mutation tests."""
    copy_root = tmp_path / "repo-copy"
    backend = copy_root / "backend"
    src = backend / "src" / "bridle" / "agent" / "container"
    src.mkdir(parents=True)
    shutil.copy2(repo_root / "backend" / "pyproject.toml", backend / "pyproject.toml")
    shutil.copy2(
        repo_root / "backend" / "src" / "bridle" / "agent" / "container" / "agent.Dockerfile",
        src / "agent.Dockerfile",
    )
    shutil.copy2(
        repo_root / "backend" / "src" / "bridle" / "agent" / "container" / "json_strict.py",
        src / "json_strict.py",
    )
    return copy_root


_SKIP_SNAPSHOT_PARTS = frozenset({"__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache"})


def _snapshot_tree(root: Path) -> dict[str, tuple[str, int]]:
    snapshot: dict[str, tuple[str, int]] = {}
    if not root.exists():
        return snapshot
    for path in sorted([root, *root.rglob("*")], key=lambda item: str(item)):
        if any(part in _SKIP_SNAPSHOT_PARTS for part in path.parts):
            continue
        if not path.exists():
            continue
        rel = path.relative_to(root).as_posix() or "."
        if path.is_file():
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            snapshot[rel] = (f"file:{digest}", path.stat().st_mode)
        elif path.is_dir():
            snapshot[rel] = ("dir", path.stat().st_mode)
    return snapshot


@contextmanager
def _read_only_tree_exact_restore(root: Path):
    saved: dict[Path, int] = {}
    for path in [root, *root.rglob("*")]:
        if not path.exists():
            continue
        saved[path] = path.stat().st_mode
        if path.is_file():
            path.chmod(stat.S_IREAD)
        elif path.is_dir():
            path.chmod(stat.S_IREAD | stat.S_IEXEC)
    try:
        yield
    finally:
        for path, mode in saved.items():
            if path.exists():
                path.chmod(mode)


def test_compute_agent_source_digest_is_stable(repo_root: Path) -> None:
    first = compute_agent_source_digest(repo_root)
    second = compute_agent_source_digest(repo_root)
    assert first == second
    assert first.startswith("sha256:")


def test_pycache_does_not_change_digest(isolated_repo_copy: Path) -> None:
    container_root = (
        isolated_repo_copy / "backend" / "src" / "bridle" / "agent" / "container"
    )
    cache_dir = container_root / "tests" / "__pycache__"
    cache_dir.mkdir(parents=True, exist_ok=True)
    pyc = cache_dir / "bridle_review_sentinel_test.cpython-312.pyc"
    pyc.write_bytes(b"fake pyc payload")
    before = compute_agent_source_digest(isolated_repo_copy)
    pyc.write_bytes(b"mutated pyc payload")
    after = compute_agent_source_digest(isolated_repo_copy)
    assert before == after


def test_real_source_tree_unchanged_after_review_suite(repo_root: Path) -> None:
    container_root = repo_root / "backend" / "src" / "bridle" / "agent" / "container"
    before = _snapshot_tree(container_root)

    first = compute_agent_source_digest(repo_root)
    second = compute_agent_source_digest(repo_root)
    paths = iter_agent_source_paths(repo_root)
    assert first == second
    assert paths
    assert all(not any(part == "__pycache__" for part in path.parts) for path in paths)

    after = _snapshot_tree(container_root)
    assert after == before


def test_read_only_isolated_copy_restores_exact_mode(isolated_repo_copy: Path) -> None:
    container_root = (
        isolated_repo_copy / "backend" / "src" / "bridle" / "agent" / "container"
    )
    target = container_root / "json_strict.py"
    readonly_file = container_root / "readonly-sentinel.txt"
    readonly_file.write_text("sentinel\n", encoding="utf-8")
    readonly_mode = stat.S_IREAD
    readonly_file.chmod(readonly_mode)
    before = _snapshot_tree(container_root)

    with _read_only_tree_exact_restore(container_root):
        digest = compute_agent_source_digest(isolated_repo_copy)
        assert digest.startswith("sha256:")

    after = _snapshot_tree(container_root)
    assert after == before
    assert readonly_file.stat().st_mode == before["readonly-sentinel.txt"][1]
    assert target.stat().st_mode == before["json_strict.py"][1]


def test_py_source_change_changes_digest_on_isolated_copy(isolated_repo_copy: Path) -> None:
    target = (
        isolated_repo_copy
        / "backend"
        / "src"
        / "bridle"
        / "agent"
        / "container"
        / "json_strict.py"
    )
    before = compute_agent_source_digest(isolated_repo_copy)
    target.write_text(target.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    after = compute_agent_source_digest(isolated_repo_copy)
    assert before != after


def test_iter_agent_source_paths_excludes_pyc(repo_root: Path) -> None:
    paths = iter_agent_source_paths(repo_root)
    assert paths
    assert all(not any(part == "__pycache__" for part in path.parts) for path in paths)
    assert all(path.suffix != ".pyc" for path in paths)


def test_verify_rejects_stale_metadata_without_docker(repo_root: Path) -> None:
    current = compute_agent_source_digest(repo_root)
    stale = "sha256:0000000000000000000000000000000000000000000000000000000000000000"

    def fake_metadata(_image: str) -> dict:
        return {
            "schema": REVIEW_METADATA_SCHEMA,
            "source_digest": stale,
            "producer": PRODUCER_VERSION,
        }

    with pytest.raises(ReviewImageError) as exc_info:
        verify_review_image(
            "bridle-agent:any",
            expected_source_digest=current,
            metadata_reader=fake_metadata,
            digest_resolver=lambda _image: "sha256:dead",
        )
    assert exc_info.value.error_code == "review_image_source_stale"


def test_find_repo_root_uses_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When bridle is installed outside the repo tree, find_repo_root uses
    BRIDLE_CANDIDATE_CONTAINER_ROOT (set by trusted controller in worker container)."""
    env_root = tmp_path / "candidate-checkout"
    (env_root / "backend" / "src").mkdir(parents=True)
    (env_root / "backend" / "pyproject.toml").write_text("[project]\nname='bridle'\n", encoding="utf-8")
    monkeypatch.setenv("BRIDLE_CANDIDATE_CONTAINER_ROOT", str(env_root))
    result = find_repo_root()
    assert result.resolve() == env_root.resolve()
