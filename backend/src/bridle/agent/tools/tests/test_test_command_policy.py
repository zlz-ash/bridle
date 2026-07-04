"""Tests for TestCommandPolicy."""
from __future__ import annotations

from bridle.agent.tools.test_command_policy import TestCommandPolicy


class TestTestCommandPolicy:
    def test_allowed_pytest(self) -> None:
        assert TestCommandPolicy.validate("pytest backend/tests/") == []

    def test_allowed_npm_run_build(self) -> None:
        assert TestCommandPolicy.validate("npm run build") == []

    def test_allowed_npm_test(self) -> None:
        assert TestCommandPolicy.validate("npm test") == []

    def test_allowed_python_m_pytest(self) -> None:
        assert TestCommandPolicy.validate("python -m pytest backend/tests") == []

    def test_rejects_rm_in_chain(self) -> None:
        errors = TestCommandPolicy.validate("pytest && rm -rf /")
        assert errors

    def test_rejects_powershell(self) -> None:
        assert TestCommandPolicy.validate("powershell -Command echo hi") != []

    def test_allows_c_drive_path(self) -> None:
        assert TestCommandPolicy.validate("pytest C:\\Windows\\temp") == []

    def test_allows_e_drive_path(self) -> None:
        assert TestCommandPolicy.validate("pytest E:\\tmp") == []

    def test_allows_path_outside_workspace(self) -> None:
        assert TestCommandPolicy.validate("pytest D:\\Other\\tests") == []

    def test_allows_bridle_subpath(self) -> None:
        assert TestCommandPolicy.validate(r"pytest D:\Bridle\backend\tests") == []

    def test_still_rejects_rm(self) -> None:
        assert TestCommandPolicy.validate("rm -rf /") != []

    def test_still_rejects_powershell(self) -> None:
        assert TestCommandPolicy.validate("powershell -Command echo hi") != []

    def test_still_rejects_npm_install(self) -> None:
        assert TestCommandPolicy.validate("npm install lodash") != []

    def test_still_rejects_curl(self) -> None:
        assert TestCommandPolicy.validate("curl http://example.com") != []

    def test_rejects_npm_install(self) -> None:
        assert TestCommandPolicy.validate("npm install lodash") != []

    def test_rejects_npm_run_dev(self) -> None:
        assert TestCommandPolicy.validate("npm run dev") != []

    def test_rejects_python_script(self) -> None:
        assert TestCommandPolicy.validate("python script.py") != []

    def test_rejects_python_c(self) -> None:
        assert TestCommandPolicy.validate('python -c "import os"') != []

