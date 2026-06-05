"""Unit tests for container node-agent runner."""
from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bridle.container_entrypoints.node_agent_inputs import NodeAgentInputs
from bridle.container_entrypoints.node_agent_runner import ContainerNodeAgentRunner
from bridle.schemas.proposal import AgentContext, AgentProposalSchema, FilePatchSchema


def _sample_inputs(tmp_path: Path) -> NodeAgentInputs:
    inputs_dir = tmp_path / "inputs"
    inputs_dir.mkdir(parents=True)
    write_root = tmp_path / "workspace" / "write"
    write_root.mkdir(parents=True)
    ctx = AgentContext(
        instruction="implement add",
        node={"id": "n1", "title": "add", "goal": "add", "node_type": "code", "depends_on": []},
        allowed_files=["add.py"],
        tests=["pytest test_add.py"],
    )
    payload = {
        "agent_context": ctx.model_dump(),
        "run_id": "run-1",
        "node_id": "db-node-1",
        "plan_node_id": "n1",
        "baseline_revision": "a" * 40,
        "read_set": [],
    }
    (inputs_dir / "context.json").write_text(json.dumps(payload), encoding="utf-8")
    return NodeAgentInputs.from_dir(inputs_dir)


@pytest.fixture
def proposal_ok() -> AgentProposalSchema:
    return AgentProposalSchema(
        summary="added add.py",
        file_patches=[
            FilePatchSchema(
                path="add.py",
                change_type="add",
                diff="--- /dev/null\n+++ b/add.py\n@@ -0,0 +1,2 @@\n+def add(a, b):\n+    return a + b\n",
            )
        ],
        tests_to_run=["pytest test_add.py"],
    )


class TestNodeAgentRunner:
    def test_execute_writes_integration_manifest(
        self,
        tmp_path: Path,
        proposal_ok: AgentProposalSchema,
    ) -> None:
        inputs = _sample_inputs(tmp_path)
        outputs = tmp_path / "output"
        runner = ContainerNodeAgentRunner(
            inputs=inputs,
            workspace_write_root=tmp_path / "workspace" / "write",
            outputs_dir=outputs,
            run_id="run-1",
            node_id="db-node-1",
        )
        test_result = {
            "status": "completed",
            "tests": [
                {
                    "name": "pytest test_add.py",
                    "command": "pytest test_add.py",
                    "status": "passed",
                    "exit_code": 0,
                    "duration_ms": 10,
                }
            ],
        }

        with (
            patch.object(runner, "_call_provider", new=AsyncMock(return_value=proposal_ok)),
            patch.object(runner, "_run_tests", new=AsyncMock(return_value=test_result)),
        ):
            manifest = asyncio.run(runner.execute())

        assert manifest["run_id"] == "run-1"
        assert manifest["node_id"] == "n1"
        assert manifest["baseline_revision"] == "a" * 40
        assert manifest["write_files"] == ["add.py"]
        assert manifest["summary"] == "added add.py"
        assert manifest["test_results"]["tests"][0]["status"] == "passed"
        assert (tmp_path / "workspace" / "write" / "add.py").exists()
        assert (outputs / "llm_trace.jsonl").exists()

    def test_provider_timeout_marks_failed(self, tmp_path: Path) -> None:
        inputs = _sample_inputs(tmp_path)
        runner = ContainerNodeAgentRunner(
            inputs=inputs,
            workspace_write_root=tmp_path / "workspace" / "write",
            outputs_dir=tmp_path / "output",
            run_id="run-1",
            node_id="db-node-1",
        )

        with patch.object(
            runner,
            "_call_provider",
            new=AsyncMock(side_effect=TimeoutError("deepseek")),
        ):
            manifest = asyncio.run(runner.execute())

        assert manifest["status"] == "failed"
        assert manifest["error_code"] == "timeout"

    def test_failed_tests_in_manifest(self, tmp_path: Path, proposal_ok: AgentProposalSchema) -> None:
        inputs = _sample_inputs(tmp_path)
        runner = ContainerNodeAgentRunner(
            inputs=inputs,
            workspace_write_root=tmp_path / "workspace" / "write",
            outputs_dir=tmp_path / "output",
            run_id="run-1",
            node_id="db-node-1",
        )
        failed_tests = {
            "status": "failed",
            "tests": [
                {
                    "name": "pytest test_add.py",
                    "command": "pytest test_add.py",
                    "status": "failed",
                    "exit_code": 1,
                    "duration_ms": 5,
                }
            ],
        }
        with (
            patch.object(runner, "_call_provider", new=AsyncMock(return_value=proposal_ok)),
            patch.object(runner, "_run_tests", new=AsyncMock(return_value=failed_tests)),
        ):
            manifest = asyncio.run(runner.execute())

        assert manifest["status"] == "failed"
        assert manifest["test_results"]["tests"][0]["exit_code"] == 1

    def test_container_entrypoints_avoid_orm_imports(self) -> None:
        root = Path(__file__).resolve().parents[2] / "src" / "bridle" / "container_entrypoints"
        banned = ("sqlalchemy", "bridle.database", "bridle.api")
        for path in root.glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        assert alias.name not in banned and not any(
                            alias.name.startswith(p) for p in banned
                        ), f"{path.name} imports {alias.name}"
                if isinstance(node, ast.ImportFrom) and node.module:
                    assert not any(node.module.startswith(p) for p in banned), (
                        f"{path.name} imports from {node.module}"
                    )
