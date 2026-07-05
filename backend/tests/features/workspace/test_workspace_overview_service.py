"""WorkspaceOverviewService unit tests."""
from __future__ import annotations

from pathlib import Path

from bridle.features.workspace.overview_service import WorkspaceOverviewService


def test_summarize_empty_workspace(test_workspace: Path) -> None:
    result = WorkspaceOverviewService.summarize(test_workspace)
    assert result == {
        "is_empty": True,
        "file_count": 0,
        "files": [],
        "excerpts": {},
    }


def test_summarize_with_readme_and_py_files(test_workspace: Path) -> None:
    (test_workspace / "README.md").write_text("# Hello\n", encoding="utf-8")
    (test_workspace / "src").mkdir(parents=True, exist_ok=True)
    for name in ("a.py", "b.py", "c.py"):
        (test_workspace / "src" / name).write_text("x", encoding="utf-8")
    result = WorkspaceOverviewService.summarize(test_workspace)
    assert result["is_empty"] is False
    assert result["file_count"] == 4
    assert "README.md" in result["files"]
    assert result["excerpts"]["README.md"] == (test_workspace / "README.md").read_bytes().decode(
        "utf-8", errors="replace"
    )[:4096]


def test_summarize_excludes_git_and_venv(test_workspace: Path) -> None:
    (test_workspace / "src" / "ok.py").parent.mkdir(parents=True, exist_ok=True)
    (test_workspace / "src" / "ok.py").write_text("ok", encoding="utf-8")
    (test_workspace / ".git" / "HEAD").parent.mkdir(parents=True, exist_ok=True)
    (test_workspace / ".git" / "HEAD").write_text("ref: main\n", encoding="utf-8")
    (test_workspace / ".venv" / "lib.py").parent.mkdir(parents=True, exist_ok=True)
    (test_workspace / ".venv" / "lib.py").write_text("secret", encoding="utf-8")
    result = WorkspaceOverviewService.summarize(test_workspace)
    assert result["file_count"] == 1
    assert result["files"] == ["src/ok.py"]


def test_summarize_truncates_long_readme(test_workspace: Path) -> None:
    (test_workspace / "README.md").write_bytes(b"x" * 5000)
    result = WorkspaceOverviewService.summarize(test_workspace, max_excerpt_bytes=4096)
    assert len(result["excerpts"]["README.md"].encode("utf-8")) == 4096


def test_summarize_non_utf8_readme(test_workspace: Path) -> None:
    (test_workspace / "README.md").write_bytes(bytes([0xD6, 0xD0, 0xCE, 0xC4]))
    result = WorkspaceOverviewService.summarize(test_workspace)
    assert "README.md" in result["excerpts"]
    assert isinstance(result["excerpts"]["README.md"], str)

