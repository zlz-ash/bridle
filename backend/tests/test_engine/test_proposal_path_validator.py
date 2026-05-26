"""Tests for ProposalPathValidator — path boundary enforcement."""
from __future__ import annotations

import pytest


class TestProposalPathValidator:
    """Unit tests for path boundary validation."""

    def test_normalize_collapses_dot_slash_and_multi_slash(self) -> None:
        from bridle.engine.proposal_path_validator import ProposalPathValidator

        n = ProposalPathValidator.normalize_workspace_relative
        assert n("./src/a.py") == "src/a.py"
        assert n("src/./a.py") == "src/a.py"
        assert n("backend//src/x.py") == "backend/src/x.py"
        assert n("./src/./sub//x.py") == "src/sub/x.py"

    def test_normalized_equivalent_matches_node_files(self) -> None:
        from bridle.engine.proposal_path_validator import ProposalPathValidator

        patches = [{"path": "src/a.py", "change_type": "modify", "diff": ""}]
        node_files = ["./src/a.py"]
        assert ProposalPathValidator.validate(patches, node_files) == []

    def test_valid_paths_pass(self) -> None:
        from bridle.engine.proposal_path_validator import ProposalPathValidator

        file_patches = [
            {"path": "src/a.py", "change_type": "modify", "diff": ""},
        ]
        node_files = ["src/a.py", "src/b.py"]
        errors = ProposalPathValidator.validate(file_patches, node_files)
        assert errors == []

    def test_multiple_valid_paths_pass(self) -> None:
        from bridle.engine.proposal_path_validator import ProposalPathValidator

        file_patches = [
            {"path": "src/a.py", "change_type": "modify", "diff": ""},
            {"path": "src/b.py", "change_type": "add", "diff": ""},
        ]
        node_files = ["src/a.py", "src/b.py"]
        errors = ProposalPathValidator.validate(file_patches, node_files)
        assert errors == []

    def test_path_not_in_node_files_fails(self) -> None:
        from bridle.engine.proposal_path_validator import ProposalPathValidator

        file_patches = [
            {"path": "src/secret.py", "change_type": "modify", "diff": ""},
        ]
        node_files = ["src/a.py"]
        errors = ProposalPathValidator.validate(file_patches, node_files)
        assert len(errors) == 1
        assert "not in node.files" in errors[0]

    def test_absolute_windows_path_fails(self) -> None:
        from bridle.engine.proposal_path_validator import ProposalPathValidator

        file_patches = [
            {"path": "C:\\secret.py", "change_type": "modify", "diff": ""},
        ]
        node_files = ["C:\\secret.py"]
        errors = ProposalPathValidator.validate(file_patches, node_files)
        assert len(errors) >= 1

    def test_absolute_posix_path_fails(self) -> None:
        from bridle.engine.proposal_path_validator import ProposalPathValidator

        file_patches = [
            {"path": "/root/secret.py", "change_type": "modify", "diff": ""},
        ]
        node_files = ["/root/secret.py"]
        errors = ProposalPathValidator.validate(file_patches, node_files)
        assert len(errors) >= 1

    def test_parent_traversal_fails(self) -> None:
        from bridle.engine.proposal_path_validator import ProposalPathValidator

        file_patches = [
            {"path": "../secret.py", "change_type": "modify", "diff": ""},
        ]
        node_files = ["../secret.py"]
        errors = ProposalPathValidator.validate(file_patches, node_files)
        assert len(errors) >= 1

    def test_backslash_bypass_fails(self) -> None:
        from bridle.engine.proposal_path_validator import ProposalPathValidator

        file_patches = [
            {"path": "src\\a.py", "change_type": "modify", "diff": ""},
        ]
        node_files = ["src/a.py"]
        errors = ProposalPathValidator.validate(file_patches, node_files)
        assert len(errors) >= 1

    def test_empty_path_fails(self) -> None:
        from bridle.engine.proposal_path_validator import ProposalPathValidator

        file_patches = [
            {"path": "", "change_type": "modify", "diff": ""},
        ]
        node_files = [""]
        errors = ProposalPathValidator.validate(file_patches, node_files)
        assert len(errors) >= 1

    def test_empty_patches_pass(self) -> None:
        from bridle.engine.proposal_path_validator import ProposalPathValidator

        errors = ProposalPathValidator.validate([], ["src/a.py"])
        assert errors == []

    def test_multiple_errors_collected(self) -> None:
        from bridle.engine.proposal_path_validator import ProposalPathValidator

        file_patches = [
            {"path": "C:\\a.py", "change_type": "modify", "diff": ""},
            {"path": "../b.py", "change_type": "add", "diff": ""},
        ]
        node_files = ["src/x.py"]
        errors = ProposalPathValidator.validate(file_patches, node_files)
        assert len(errors) >= 2

    def test_subdir_path_valid(self) -> None:
        from bridle.engine.proposal_path_validator import ProposalPathValidator

        file_patches = [
            {"path": "backend/src/example.py", "change_type": "modify", "diff": ""},
        ]
        node_files = ["backend/src/example.py"]
        errors = ProposalPathValidator.validate(file_patches, node_files)
        assert errors == []

    def test_first_offending_patch_path(self) -> None:
        from bridle.engine.proposal_path_validator import ProposalPathValidator

        assert ProposalPathValidator.first_offending_patch_path([], ["src/a.py"]) is None
        patches = [
            {"path": "src/a.py", "change_type": "modify", "diff": ""},
            {"path": "../b.py", "change_type": "modify", "diff": ""},
        ]
        assert ProposalPathValidator.first_offending_patch_path(patches, ["src/a.py"]) == "../b.py"
