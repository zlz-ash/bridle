from __future__ import annotations

from typer.testing import CliRunner

from bridle.agent.tools.registry import AgentToolRegistry
from bridle.cli import app


def test_cli_exposes_only_thin_service_adapters() -> None:
    runner = CliRunner()
    root = runner.invoke(app, ["--help"])
    code = runner.invoke(app, ["code", "--help"])

    assert root.exit_code == 0
    for command in ("code", "plan", "agent", "candidate", "verify", "serve", "obs"):
        assert command in root.output
    assert code.exit_code == 0
    for command in ("inspect", "search", "graph"):
        assert command in code.output
    forbidden_cli = ("patch", "apply-diff", "run-command")
    assert not any(command in root.output or command in code.output for command in forbidden_cli)

    names = {item.name for item in AgentToolRegistry.tool_descriptors()}
    assert {"run_command", "report_blocked", "web_search"} <= names
    assert not {
        "read_allowed_file",
        "grep_code",
        "read_code_map",
        "propose_file_patch",
        "run_allowed_tests",
        "select_node",
    } & names
