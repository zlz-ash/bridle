"""Tests for candidate path guard fail-closed semantics."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from bridle.agent.container.candidate_path_guard import CandidatePathError, validate_candidate_rel


@pytest.mark.parametrize(
    "candidate_rel",
    [
        "/candidates/foo",
        "\\candidates\\foo",
        "C:/candidates/foo",
        "C:\\candidates\\foo",
        "//server/share/candidates/foo",
        "\\\\server\\share\\candidates\\foo",
        "candidates//foo",
        "candidates/./foo",
        "candidates/../foo",
        "",
    ],
)
def test_rejects_absolute_and_malformed_candidate_rel(candidate_rel: str) -> None:
    with pytest.raises(CandidatePathError, match="candidate_rel"):
        validate_candidate_rel(candidate_rel)


def test_accepts_valid_candidate_rel() -> None:
    assert validate_candidate_rel("candidates/cand-1") == "candidates/cand-1"
    assert validate_candidate_rel(" candidates/cand-1 ") == "candidates/cand-1"


@pytest.mark.skipif(os.name != "nt", reason="Windows junction test")
def test_windows_junction_not_followed_for_delete(test_workspace: Path) -> None:
    from bridle.agent.container.candidate_path_guard import safe_rmtree

    module_root = test_workspace / ".bridle" / "runtime" / "modules" / "junction-mod"
    cand_a = module_root / "candidates" / "cand-a"
    cand_b = module_root / "candidates" / "cand-b" / "project"
    cand_a.mkdir(parents=True)
    cand_b.mkdir(parents=True)
    sentinel = cand_b / "sentinel.txt"
    sentinel.write_text("keep\n", encoding="utf-8")

    link = cand_a / "project"
    if link.exists():
        safe_rmtree(link, project_root=test_workspace, expected_root=cand_a)
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(cand_b)],
        check=True,
        capture_output=True,
    )
    with pytest.raises(CandidatePathError, match="refuse_symlink_or_reparse"):
        safe_rmtree(link, project_root=test_workspace, expected_root=cand_a)
    assert sentinel.read_text(encoding="utf-8") == "keep\n"
