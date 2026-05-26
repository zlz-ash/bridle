"""Tests for TestCommandPolicy."""
from __future__ import annotations

from bridle.engine.test_command_policy import TestCommandPolicy


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

    def test_rejects_c_drive_path(self) -> None:
        errors = TestCommandPolicy.validate("pytest C:\\Windows\\temp")
        assert any("C:" in e for e in errors)

    def test_rejects_c_drive_even_with_workspace_path(self) -> None:
        errors = TestCommandPolicy.validate("pytest C:\\Temp D:\\Bridle\\backend\\tests")
        assert any("C:" in e for e in errors)

    def test_allows_bridle_subpath(self) -> None:
        assert TestCommandPolicy.validate(r"pytest D:\Bridle\backend\tests") == []

    def test_rejects_path_outside_workspace_on_d_drive(self) -> None:
        errors = TestCommandPolicy.validate("pytest D:\\Other\\tests")
        assert errors

    def test_rejects_e_drive_path(self) -> None:
        errors = TestCommandPolicy.validate("pytest E:\\tmp")
        assert errors

    def test_rejects_npm_install(self) -> None:
        assert TestCommandPolicy.validate("npm install lodash") != []

    def test_rejects_npm_run_dev(self) -> None:
        assert TestCommandPolicy.validate("npm run dev") != []

    def test_rejects_python_script(self) -> None:
        assert TestCommandPolicy.validate("python script.py") != []

    def test_rejects_python_c(self) -> None:
        assert TestCommandPolicy.validate('python -c "import os"') != []
