"""CLI Unicode and version contract tests."""
from __future__ import annotations

from typer.testing import CliRunner

from bridle import __version__
from bridle.app import create_app
from bridle.cli import app


def test_serve_help_contains_real_chinese_text() -> None:
    result = CliRunner().invoke(app, ["serve", "--help"])
    escaped = result.output.encode("unicode_escape").decode("ascii")

    assert result.exit_code == 0
    assert "\\u4e0d\\u81ea\\u52a8\\u628a workspace" in escaped
    assert "\\u521d\\u59cb\\u5316\\u4e3a git" in escaped
    assert "\\u4ed3\\u5e93\\uff08\\u9ad8\\u7ea7\\u7528\\u6237\\uff09" in escaped
    assert "\\ue750" not in escaped
    assert "\\u6d93\\u5d88" not in escaped


def test_cli_and_openapi_use_package_version() -> None:
    result = CliRunner().invoke(app, ["version"])
    fastapi_app = create_app()

    assert result.exit_code == 0
    assert result.output.strip() == f"bridle {__version__}"
    assert fastapi_app.version == __version__
    assert fastapi_app.openapi()["info"]["version"] == __version__
