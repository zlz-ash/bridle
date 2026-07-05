import hashlib
import json
from pathlib import Path

import pytest

from bridle.agent.providers.agent_provider import AgentProviderFactory
from bridle.agent.runtime.schemas import AgentProposalSchema
from bridle.features.project_map.store import ProjectPlanStore


def _hash_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class _CaptureProvider:
    """Return deterministic assistant text; context input exits into the test capture list."""

    name = "capture"

    def __init__(self, captured: list, handlers: dict, tool_results: list) -> None:
        self._captured = captured
        self._handlers = handlers
        self._tool_results = tool_results

    async def generate(self, context):
        """Capture one bounded runtime context; provider input exits as a valid proposal response."""
        self._captured.append(context)
        if len(self._captured) == 1:
            self._tool_results.append(await self._handlers["read_project_map"]({"mode": "overview"}))
            self._tool_results.append(await self._handlers["patch_plan_nodes"]({
                "add_nodes": [{
                    "id": "planned-node",
                    "title": "Planned node",
                    "goal": "Implement the planned node",
                    "node_type": "code_change",
                    "files": ["src/example.py"],
                    "tests": ["pytest tests/test_example.py -q"],
                }],
            }))
        return AgentProposalSchema(summary=f"reply-{len(self._captured)}")


@pytest.mark.asyncio
async def test_same_session_keeps_messages_tools_skills_and_memory_across_roles(
    client,
    test_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Converse across roles; project/session inputs exit with continuous shared runtime context."""
    root = test_workspace / "unified-runtime"
    root.mkdir()
    example = root / "src" / "example.py"
    example.parent.mkdir(parents=True, exist_ok=True)
    example.write_text("# example\n", encoding="utf-8")
    test_file = root / "tests" / "test_example.py"
    test_file.parent.mkdir(parents=True, exist_ok=True)
    test_file.write_text("def test_example():\n    assert True\n", encoding="utf-8")
    project = (await client.post("/api/v1/projects/open", json={"path": str(root)})).json()
    session = (
        await client.post(
            "/api/v1/sessions",
            json={"project_id": project["id"], "title": "Shared runtime"},
        )
    ).json()
    captured = []
    tool_results = []

    def create_provider(context=None, **kwargs):
        """Assert provider construction has context; context input exits as one shared capture provider."""
        assert context is not None
        handlers = kwargs["runtime_tool_handlers"]
        assert set(handlers) == {
            "read_project_map",
            "read_code_map",
            "patch_plan_nodes",
            "select_node",
            "propose_semantic_annotation",
            "dispatch_child_agent",
        }
        return _CaptureProvider(captured, handlers, tool_results)

    monkeypatch.setattr(AgentProviderFactory, "create", staticmethod(create_provider))

    first = await client.post(
        f"/api/v1/sessions/{session['id']}/converse",
        json={"content": "plan the change"},
    )
    changed = await client.post(
        f"/api/v1/sessions/{session['id']}/role",
        json={"role": "executing", "actor": "user", "confirmed": True},
    )
    formal_hash_before = _hash_file(example)

    second = await client.post(
        f"/api/v1/sessions/{session['id']}/converse",
        json={"content": "execute the next node", "node_id": "planned-node"},
    )
    assert first.status_code == 201
    assert changed.status_code == 200
    assert second.status_code == 201, second.text
    history = await client.get(f"/api/v1/sessions/{session['id']}/messages")
    overview = await client.get(f"/api/v1/projects/{project['id']}/map/overview")
    node = await client.get(f"/api/v1/projects/{project['id']}/map/nodes/planned-node")

    assert first.json()["content"] == "reply-1"
    assert changed.json()["role"] == "executing"
    assert second.json()["content"] == "reply-2"
    assert [message["role"] for message in history.json()] == [
        "user", "assistant", "user", "assistant",
    ]
    assert captured[0].accessible_context["memory"][-1]["content"] == "plan the change"
    assert any(
        message["content"] == "reply-1"
        for message in captured[1].accessible_context["memory"]
    )
    assert captured[0].accessible_context["skill_ids"] == captured[1].accessible_context["skill_ids"]
    assert set(captured[0].tool_capabilities) == set(captured[1].tool_capabilities)
    assert captured[0].tool_capabilities != captured[1].tool_capabilities
    assert tool_results[0]["plan_node_count"] == 0
    assert tool_results[1]["changed_node_ids"] == ["planned-node"]
    assert captured[1].node["id"] == "planned-node"
    assert "src/example.py" in captured[1].allowed_files
    assert captured[1].tests == ["pytest tests/test_example.py -q"]
    sandbox = captured[1].tool_capabilities["sandbox"]
    assert "src/example.py" in sandbox["allowed_files"]
    assert sandbox["candidate_id"] is not None
    assert Path(sandbox["workspace_root"]).name == "project"
    assert "candidates" in sandbox["workspace_root"]
    assert _hash_file(example) == formal_hash_before
    module_id = captured[1].node.get("module_id") or "planned-node"
    candidate_root = (
        root / ".bridle" / "runtime" / "modules" / module_id / "candidates" / sandbox["candidate_id"]
    )
    assert candidate_root.is_dir()
    assert (candidate_root / "result.json").is_file()
    result_payload = json.loads((candidate_root / "result.json").read_text(encoding="utf-8"))
    assert result_payload["candidate_id"] == sandbox["candidate_id"]
    assert result_payload["status"] == "blocked"
    assert result_payload["error_code"] == "verification_incomplete"
    assert result_payload.get("verification") is not None
    assert result_payload["verification"]["all_required_passed"] is False
    assert captured[0].tool_capabilities["sandbox"].get("candidate_id") is None
    assert overview.json()["plan_node_count"] == 1
    assert node.json()["status"] == "running"


@pytest.mark.asyncio
async def test_executing_provider_error_persists_blocked_result(
    client,
    test_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = test_workspace / "provider-error-runtime"
    root.mkdir()
    example = root / "src" / "example.py"
    example.parent.mkdir(parents=True, exist_ok=True)
    example.write_text("# example\n", encoding="utf-8")
    (root / "tests" / "test_example.py").parent.mkdir(parents=True, exist_ok=True)
    (root / "tests" / "test_example.py").write_text("def test_example():\n    assert True\n", encoding="utf-8")

    project = (await client.post("/api/v1/projects/open", json={"path": str(root)})).json()
    session = (await client.post("/api/v1/sessions", json={"project_id": project["id"]})).json()
    captured: list = []

    class _BoomProvider:
        name = "boom"

        async def generate(self, context):
            raise RuntimeError("provider_exploded")

    def create_provider(context=None, **kwargs):
        captured.append(context)
        if len(captured) == 1:
            handlers = kwargs["runtime_tool_handlers"]

            class _PlanOnce:
                name = "capture-once"

                async def generate(self, ctx):
                    await handlers["patch_plan_nodes"]({
                        "add_nodes": [{
                            "id": "planned-node",
                            "title": "Planned node",
                            "goal": "Implement",
                            "node_type": "code_change",
                            "files": ["src/example.py"],
                            "tests": ["pytest tests/test_example.py -q"],
                        }],
                    })
                    return AgentProposalSchema(summary="reply-1")

            return _PlanOnce()
        return _BoomProvider()

    monkeypatch.setattr(
        "bridle.agent.providers.agent_provider.AgentProviderFactory.create",
        staticmethod(create_provider),
    )

    await client.post(f"/api/v1/sessions/{session['id']}/converse", json={"content": "plan"})
    await client.post(
        f"/api/v1/sessions/{session['id']}/role",
        json={"role": "executing", "actor": "user", "confirmed": True},
    )
    with pytest.raises(RuntimeError, match="provider_exploded"):
        await client.post(
            f"/api/v1/sessions/{session['id']}/converse",
            json={"content": "execute", "node_id": "planned-node"},
        )
    candidate_roots = [
        path
        for path in (root / ".bridle" / "runtime" / "modules").rglob("candidates/*")
        if path.is_dir() and (path / "result.json").is_file()
    ]
    assert candidate_roots
    result_payload = json.loads((candidate_roots[0] / "result.json").read_text(encoding="utf-8"))
    assert result_payload["status"] == "blocked"
    assert result_payload["error_code"] == "RuntimeError"


@pytest.mark.asyncio
async def test_executing_conversation_requires_an_explicit_plan_node(
    client,
    test_workspace: Path,
) -> None:
    """Send an executing turn without a node; request input exits as a fail-closed rejection."""
    root = test_workspace / "execution-node-required"
    root.mkdir()
    project = (await client.post("/api/v1/projects/open", json={"path": str(root)})).json()
    session = (
        await client.post("/api/v1/sessions", json={"project_id": project["id"]})
    ).json()
    await client.post(
        f"/api/v1/sessions/{session['id']}/role",
        json={"role": "executing", "actor": "user", "confirmed": True},
    )

    response = await client.post(
        f"/api/v1/sessions/{session['id']}/converse",
        json={"content": "execute"},
    )

    assert response.status_code == 409
    assert response.json()["code"] == "execution_node_required"


@pytest.mark.asyncio
async def test_main_conversation_requires_ready_project_map(
    client,
    test_workspace: Path,
) -> None:
    """Converse before map readiness; session input exits with a structured map gate rejection."""
    root = test_workspace / "not-ready-runtime"
    root.mkdir()
    project = (await client.post("/api/v1/projects/open", json={"path": str(root)})).json()
    ProjectPlanStore(root, project_id=project["id"]).mark_map_status(
        "needs_arbitration",
        reason="pending_user_decision",
    )
    session = (
        await client.post("/api/v1/sessions", json={"project_id": project["id"]})
    ).json()

    response = await client.post(
        f"/api/v1/sessions/{session['id']}/converse",
        json={"content": "can we plan?"},
    )

    assert response.status_code == 409
    assert response.json()["code"] == "project_map_not_ready"
    assert response.json()["details"]["scan_status"] == "needs_arbitration"

