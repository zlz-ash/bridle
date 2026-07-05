"""Tests for TDD path mapping heuristics."""
from __future__ import annotations

import pytest

from bridle.agent.tools.tdd_paths import (
    derive_test_path,
    expand_allowed_files_for_tdd,
    is_test_path,
)


class TestIsTestPath:
    @pytest.mark.parametrize(
        "path",
        [
            "tests/test_foo.py",
            "tests/sub/test_bar.py",
            "backend/tests/test_baz.py",
            "test/test_legacy.py",
            "src/module_test.py",
            "src/foo/test_bar.py",
            "tests/conftest.py",  # under tests/ counts even without test_ prefix
        ],
    )
    def test_recognized_as_test(self, path: str) -> None:
        assert is_test_path(path)

    @pytest.mark.parametrize(
        "path",
        [
            "src/foo.py",
            "src/converter.py",
            "backend/src/bridle/engine/tdd_paths.py",
            "lib/utils.py",
            "README.md",
            "",
        ],
    )
    def test_not_a_test(self, path: str) -> None:
        assert not is_test_path(path)

    def test_windows_separator_normalized(self) -> None:
        assert is_test_path("tests\\test_foo.py")

    def test_leading_slash_stripped(self) -> None:
        assert is_test_path("/tests/test_foo.py")


class TestDeriveTestPath:
    def test_src_root(self) -> None:
        assert derive_test_path("src/converter.py") == "tests/test_converter.py"

    def test_src_subdir(self) -> None:
        assert derive_test_path("src/foo/bar.py") == "tests/foo/test_bar.py"

    def test_backend_src(self) -> None:
        assert (
            derive_test_path("backend/src/bridle/engine/sandbox_policy.py")
            == "backend/tests/engine/test_sandbox_policy.py"
        )

    def test_no_prefix_fallback(self) -> None:
        assert derive_test_path("a/b/c.py") == "tests/test_c.py"

    def test_already_test_returns_self(self) -> None:
        assert derive_test_path("tests/test_x.py") == "tests/test_x.py"

    def test_non_python_returns_none(self) -> None:
        assert derive_test_path("src/foo.txt") is None
        assert derive_test_path("README.md") is None
        assert derive_test_path("") is None


class TestExpandAllowedFiles:
    def test_appends_test_for_src(self) -> None:
        out = expand_allowed_files_for_tdd(["src/converter.py"])
        assert out == ["src/converter.py", "tests/test_converter.py"]

    def test_test_files_kept_as_is(self) -> None:
        out = expand_allowed_files_for_tdd(["tests/test_converter.py"])
        assert out == ["tests/test_converter.py"]

    def test_mixed_inputs_deduped(self) -> None:
        out = expand_allowed_files_for_tdd(
            ["src/converter.py", "tests/test_converter.py", "src/converter.py"]
        )
        assert out == ["src/converter.py", "tests/test_converter.py"]

    def test_non_python_passes_through(self) -> None:
        out = expand_allowed_files_for_tdd(["src/foo.py", "config.json"])
        assert out == ["src/foo.py", "config.json", "tests/test_foo.py"]

    def test_preserves_order(self) -> None:
        out = expand_allowed_files_for_tdd(["src/a.py", "src/b.py"])
        assert out == ["src/a.py", "src/b.py", "tests/test_a.py", "tests/test_b.py"]

