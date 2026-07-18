import asyncio
import hashlib
import json
import sqlite3
import threading
from contextlib import closing
from pathlib import Path

import pytest
from sqlalchemy import func, select

import bridle.agent.runtime.gateway as gateway_module
import bridle.logging.facade as logging_facade_module
from bridle.agent.container.boundary import compute_boundary_fingerprint
from bridle.agent.container.candidate_service import CandidateExecutionService
from bridle.agent.container.container_service import configure_runner
from bridle.agent.container.image_identity import resolve_image_identity
from bridle.agent.providers.agent_provider import AgentProviderFactory
from bridle.agent.runtime.mailbox import MailboxResult
from bridle.agent.runtime.modification_workflow import (
    ModificationState,
    ModificationWorkflow,
)
from bridle.agent.runtime.project_registry import reset_project_runtime_registry_for_tests
from bridle.agent.runtime.schemas import AgentProposalSchema
from bridle.features.project_map.store import ProjectPlanStore
from bridle.logging.facade import LoggingFacade
from bridle.logging.schema import LogEvent
from bridle.models.agent_runtime import (
    AgentRuntimeRecord,
    RuntimeInputResultRecord,
)
from tests.helpers.verification_fixtures import (
    PassingStructuredRunner,
    advance_to_implementing,
    freeze_contract_for_candidate_identity,
)


def _hash_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


async def _open_ready_project(client, root: Path) -> dict:
    response = await client.post("/api/v1/projects/open", json={"path": str(root)})
    assert response.status_code == 200, response.text
    project = response.json()
    store = ProjectPlanStore(root, project_id=project["id"])
    store.rescan()
    store.run_semantic_scan()
    assert store.readiness()["scan_status"] == "ready"
    return project


async def _restart_gateway_for_test() -> None:
    await gateway_module.shutdown_gateway_runtimes()
    reset_project_runtime_registry_for_tests()


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
                }, {
                    "id": "child-node",
                    "title": "Child node",
                    "goal": "Map one child task",
                    "node_type": "research",
                    "files": [],
                    "tests": [],
                }],
            }))
            self._tool_results.append(
                await self._handlers["dispatch_child_agent"](
                    {"node_id": "child-node", "target_role": "mapping"}
                )
            )
        else:
            candidate_root = Path(
                context.tool_capabilities["sandbox"]["workspace_root"]
            )
            (candidate_root / "src" / "example.py").write_text(
                "VALUE = 1\n",
                encoding="utf-8",
            )
        return AgentProposalSchema(summary=f"reply-{len(self._captured)}")


class _DispatchProvider:
    """Create and dispatch real plan nodes through the Gateway's production tool handlers."""

    name = "dispatch"

    def __init__(
        self,
        handlers: dict,
        node_ids: tuple[str, ...],
        *,
        dispatched: asyncio.Event | None = None,
        release: asyncio.Event | None = None,
    ) -> None:
        self._handlers = handlers
        self._node_ids = node_ids
        self._dispatched = dispatched
        self._release = release

    async def generate(self, _context):
        await self._handlers["patch_plan_nodes"](
            {
                "add_nodes": [
                    {
                        "id": node_id,
                        "title": node_id,
                        "goal": f"Run {node_id}",
                        "node_type": "research",
                        "files": [],
                        "tests": [],
                    }
                    for node_id in self._node_ids
                ]
            }
        )
        for node_id in self._node_ids:
            await self._handlers["dispatch_child_agent"](
                {"node_id": node_id, "target_role": "mapping"}
            )
        if self._dispatched is not None:
            self._dispatched.set()
        if self._release is not None:
            await self._release.wait()
        return AgentProposalSchema(summary="dispatched")


class _InstructionProvider:
    name = "instruction"

    def __init__(self, instruction: str, execution_order: list[str]) -> None:
        self._instruction = instruction
        self._execution_order = execution_order

    async def generate(self, _context):
        self._execution_order.append(self._instruction)
        return AgentProposalSchema(summary=f"reply:{self._instruction}")


class _EventSink:
    def __init__(self) -> None:
        self.events: list[LogEvent] = []

    def emit(self, event: LogEvent) -> None:
        self.events.append(event)


@pytest.mark.asyncio
async def test_same_session_keeps_messages_tools_skills_and_memory_across_roles(
    client,
    db,
    test_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Converse across roles; project/session inputs exit with continuous shared runtime context."""
    caplog.set_level(
        "INFO",
        logger="bridle.agent.runtime.verification_orchestrator",
    )
    root = test_workspace / "unified-runtime"
    root.mkdir()
    example = root / "src" / "example.py"
    example.parent.mkdir(parents=True, exist_ok=True)
    example.write_text("# example\n", encoding="utf-8")
    test_file = root / "tests" / "test_example.py"
    test_file.parent.mkdir(parents=True, exist_ok=True)
    test_file.write_text("def test_example():\n    assert True\n", encoding="utf-8")
    project = await _open_ready_project(client, root)
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
            "read_project_map",
            "patch_plan_nodes",
            "execute_plan_node",
            "propose_semantic_annotation",
            "dispatch_child_agent",
        }
        return _CaptureProvider(captured, handlers, tool_results)

    monkeypatch.setattr(AgentProviderFactory, "create", staticmethod(create_provider))

    first = await client.post(
        f"/api/v1/sessions/{session['id']}/converse",
        json={"content": "plan the change"},
    )
    assert first.status_code == 201
    store = ProjectPlanStore(root, project_id=project["id"])
    snapshot = store.module_execution_snapshot("planned-node")
    workflow = ModificationWorkflow(store)
    contract = freeze_contract_for_candidate_identity(
        workflow,
        "planned-node",
        project_root=root,
        test_commands=list(snapshot["test_commands"]),
        test_paths=[item["path"] for item in snapshot["test_entities"]],
        map_seq=store.latest_change_seq(),
        boundary_fingerprint=compute_boundary_fingerprint(
            module_id=str(snapshot["module_id"]),
            implementation_entities=list(snapshot["implementation_entities"]),
            test_entities=list(snapshot["test_entities"]),
            interfaces=list(snapshot.get("interfaces") or []),
            readonly_files=[],
            test_dir=snapshot.get("test_dir"),
        ),
        image_version=resolve_image_identity(CandidateExecutionService.DEFAULT_IMAGE),
    )
    advance_to_implementing(workflow, "planned-node", contract)
    runner = PassingStructuredRunner(root)
    configure_runner(root, runner)
    changed = await client.post(
        f"/api/v1/sessions/{session['id']}/role",
        json={"role": "executing", "actor": "user", "confirmed": True},
    )
    formal_hash_before = _hash_file(example)

    second = await client.post(
        f"/api/v1/sessions/{session['id']}/converse",
        json={"content": "execute the next node", "node_id": "planned-node"},
    )
    assert changed.status_code == 200
    assert second.status_code == 201, second.text

    assert first.json()["content"] == "reply-1"
    assert changed.json()["role"] == "executing"
    assert second.json()["content"] == "reply-2"
    assert captured[0].accessible_context["memory"][-1]["content"] == "plan the change"
    assert any(
        message["content"] == "reply-1"
        for message in captured[1].accessible_context["memory"]
    )
    assert captured[0].accessible_context["skill_ids"] == captured[1].accessible_context["skill_ids"]
    assert set(captured[0].tool_capabilities) == set(captured[1].tool_capabilities)
    assert captured[0].tool_capabilities != captured[1].tool_capabilities
    assert tool_results[0]["plan_node_count"] == 0
    assert tool_results[1]["changed_node_ids"] == ["child-node", "planned-node"]
    assert tool_results[2]["runtime_id"]
    assert captured[1].node["id"] == "planned-node"
    assert "src/example.py" in captured[1].allowed_files
    assert captured[1].tests == ["pytest tests/test_example.py -q"]
    sandbox = captured[1].tool_capabilities["sandbox"]
    assert "src/example.py" in sandbox["allowed_files"]
    assert sandbox["candidate_id"] is not None
    assert Path(sandbox["workspace_root"]).name == "project"
    assert "candidates" in sandbox["workspace_root"]
    assert (Path(sandbox["workspace_root"]) / "src" / "example.py").read_text(
        encoding="utf-8"
    ) == "VALUE = 1\n"
    assert _hash_file(example) == formal_hash_before
    parent_count = int(
        (
            await db.execute(
                select(func.count())
                .select_from(AgentRuntimeRecord)
                .where(AgentRuntimeRecord.runtime_type == "parent")
            )
        ).scalar_one()
    )
    child_count = int(
        (
            await db.execute(
                select(func.count())
                .select_from(AgentRuntimeRecord)
                .where(AgentRuntimeRecord.runtime_type == "child")
            )
        ).scalar_one()
    )
    result_count = int(
        (
            await db.execute(select(func.count()).select_from(RuntimeInputResultRecord))
        ).scalar_one()
    )
    assert (parent_count, child_count, result_count) == (1, 1, 2)
    child_result_message_id = f"child-result-{tool_results[2]['spawn_message_id']}"
    for _ in range(100):
        with closing(sqlite3.connect(root / ".bridle" / "plan.db")) as connection:
            receipt = connection.execute(
                "SELECT result_status FROM child_result_receipts WHERE message_id=?",
                (child_result_message_id,),
            ).fetchone()
        if receipt is not None:
            break
        await asyncio.sleep(0.01)
    assert receipt == ("completed",)
    assert ProjectPlanStore(root, project_id=project["id"]).get_node("child-node")["status"] == "completed"
    with closing(sqlite3.connect(root / ".bridle" / "mail.db")) as connection:
        runtime_inputs = int(
            connection.execute(
                "SELECT COUNT(*) FROM mail_messages WHERE message_type='runtime-input'"
            ).fetchone()[0]
        )
        child_result_status = connection.execute(
            "SELECT status FROM mail_messages WHERE message_id=?",
            (child_result_message_id,),
        ).fetchone()[0]
    assert runtime_inputs == 2
    assert child_result_status == "delivered"
    module_id = captured[1].node.get("module_id") or "planned-node"
    candidate_root = (
        root / ".bridle" / "runtime" / "modules" / module_id / "candidates" / sandbox["candidate_id"]
    )
    assert candidate_root.is_dir()
    result_path = candidate_root / "result.json"
    for _ in range(300):
        current = ModificationWorkflow(
            ProjectPlanStore.open_existing(root)
        ).get("planned-node")
        result_payload = (
            json.loads(result_path.read_text(encoding="utf-8"))
            if result_path.is_file()
            else None
        )
        if (
            current["state"] == ModificationState.READY_TO_PUBLISH.value
            and result_payload is not None
            and result_payload["status"] == "ready"
        ):
            break
        await asyncio.sleep(0.01)
    assert current["state"] == ModificationState.READY_TO_PUBLISH.value
    assert result_payload is not None
    assert result_payload["candidate_id"] == sandbox["candidate_id"]
    assert result_payload["status"] == "ready"
    assert result_payload["error_code"] is None
    assert result_payload["verification"]["status"] == "passed"
    assert len(runner.executions) == 1
    submitted_events = [
        item
        for item in workflow.events("planned-node")
        if item["event"] == "submitted"
    ]
    assert len(submitted_events) == 1
    assert submitted_events[0]["payload"]["candidate_id"] == sandbox["candidate_id"]
    assert (
        submitted_events[0]["payload"]["test_contract_version"]
        == contract.contract_version
    )
    assert list(
        (root / ".bridle" / "runtime" / "modules").rglob("result.json")
    ) == [result_path]
    verification_run = store.latest_verification_run("planned-node")
    assert verification_run is not None
    assert verification_run["candidate_id"] == sandbox["candidate_id"]
    assert verification_run["phase"] == "final"
    assert verification_run["state"] == "completed"
    assert verification_run["outcome"]["event"] == "final_verification_passed"
    assert verification_run["outcome"]["status"] == "passed"
    assert result_payload["verification"]["run_id"] == verification_run["run_id"]
    result_logs = [
        record
        for record in caplog.records
        if getattr(record, "action", None) == "candidate_result_persisted"
    ]
    assert len(result_logs) == 1
    assert result_logs[0].detail == {
        "run_id": verification_run["run_id"],
        "node_id": "planned-node",
        "candidate_id": sandbox["candidate_id"],
        "attempt": 1,
        "status": "ready",
        "duration_ms": result_payload["verification"]["duration_ms"],
        "error_code": None,
    }
    history = await client.get(f"/api/v1/sessions/{session['id']}/messages")
    overview = await client.get(f"/api/v1/projects/{project['id']}/map/overview")
    node = await client.get(
        f"/api/v1/projects/{project['id']}/map/nodes/planned-node"
    )
    assert [message["role"] for message in history.json()] == [
        "user", "assistant", "user", "assistant",
    ]
    assert captured[0].tool_capabilities["sandbox"].get("candidate_id") is None
    assert overview.json()["plan_node_count"] == 2
    assert node.json()["status"] == "running"

    closed = await client.post(f"/api/v1/sessions/{session['id']}/close")
    closed_history = await client.get(f"/api/v1/sessions/{session['id']}/messages")
    rejected = await client.post(
        f"/api/v1/sessions/{session['id']}/converse",
        json={"content": "must not run", "node_id": "planned-node"},
    )
    assert closed.status_code == 200
    assert closed.json()["status"] == "closed"
    assert closed_history.json() == history.json()
    assert rejected.status_code == 409
    assert all(
        handle.spec.session_id != session["id"]
        for _, host, _ in gateway_module._runtime_components.values()
        for handle in host.active_handles()
    )
    db.expire_all()
    runtime_rows = (
        await db.execute(
            select(AgentRuntimeRecord).where(AgentRuntimeRecord.session_id == session["id"])
        )
    ).scalars().all()
    assert runtime_rows
    assert {record.status for record in runtime_rows} == {"DESTROYED"}


@pytest.mark.asyncio
async def test_gateway_runs_multiple_children_and_applies_each_durable_result(
    client,
    db,
    test_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = test_workspace / "multiple-child-runtime"
    root.mkdir()
    project = await _open_ready_project(client, root)
    session = (
        await client.post(
            "/api/v1/sessions",
            json={"project_id": project["id"], "title": "Multiple children"},
        )
    ).json()

    def create_provider(context=None, **kwargs):
        assert context is not None
        return _DispatchProvider(kwargs["runtime_tool_handlers"], ("child-a", "child-b"))

    monkeypatch.setattr(AgentProviderFactory, "create", staticmethod(create_provider))
    response = await client.post(
        f"/api/v1/sessions/{session['id']}/converse",
        json={"content": "dispatch two"},
    )
    assert response.status_code == 201, response.text
    store = ProjectPlanStore(root, project_id=project["id"])
    assert [store.get_node(node_id)["status"] for node_id in ("child-a", "child-b")] == [
        "completed",
        "completed",
    ]
    with closing(sqlite3.connect(root / ".bridle" / "plan.db")) as connection:
        receipts = connection.execute(
            "SELECT result_status, result_json FROM child_result_receipts ORDER BY message_id"
        ).fetchall()
    assert [receipt[0] for receipt in receipts] == ["completed", "completed"]
    child_outputs = [json.loads(receipt[1]) for receipt in receipts]
    assert {output["node_id"] for output in child_outputs} == {"child-a", "child-b"}
    assert all(output["target_role"] == "mapping" for output in child_outputs)
    assert all(
        output["node_id"] in {node["id"] for node in output["result"]["nodes"]}
        for output in child_outputs
    )
    child_count = int(
        (
            await db.execute(
                select(func.count())
                .select_from(AgentRuntimeRecord)
                .where(AgentRuntimeRecord.runtime_type == "child")
            )
        ).scalar_one()
    )
    assert child_count == 2
    await client.post(f"/api/v1/sessions/{session['id']}/close")


@pytest.mark.asyncio
async def test_gateway_child_failure_is_applied_and_acked_before_destroy(
    client,
    test_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = test_workspace / "failed-child-runtime"
    root.mkdir()
    project = await _open_ready_project(client, root)
    session = (
        await client.post(
            "/api/v1/sessions",
            json={"project_id": project["id"], "title": "Failed child"},
        )
    ).json()

    def create_provider(context=None, **kwargs):
        assert context is not None
        return _DispatchProvider(kwargs["runtime_tool_handlers"], ("child-failed",))

    async def fail_child_work(_store, *, node_id: str, target_role: str) -> dict:
        raise RuntimeError(f"failed:{node_id}:{target_role}")

    monkeypatch.setattr(AgentProviderFactory, "create", staticmethod(create_provider))
    monkeypatch.setattr(gateway_module, "_execute_child_work", fail_child_work)
    response = await client.post(
        f"/api/v1/sessions/{session['id']}/converse",
        json={"content": "dispatch failing child"},
    )
    assert response.status_code == 201, response.text
    store = ProjectPlanStore(root, project_id=project["id"])
    assert store.get_node("child-failed")["status"] == "failed"
    with closing(sqlite3.connect(root / ".bridle" / "plan.db")) as connection:
        receipt = connection.execute(
            "SELECT result_status, result_json FROM child_result_receipts"
        ).fetchone()
    assert receipt is not None
    assert receipt[0] == "failed"
    assert json.loads(receipt[1]) == {
        "error_code": "RuntimeError",
        "message": "failed:child-failed:mapping",
    }
    with closing(sqlite3.connect(root / ".bridle" / "mail.db")) as connection:
        child_mail = connection.execute(
            "SELECT status FROM mail_messages WHERE message_type='child-result'"
        ).fetchone()
    assert child_mail == ("delivered",)
    await client.post(f"/api/v1/sessions/{session['id']}/close")


@pytest.mark.asyncio
async def test_gateway_real_execution_snapshot_error_fails_child_and_persists_code(
    client,
    test_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _restart_gateway_for_test()
    sink = _EventSink()
    monkeypatch.setattr(
        logging_facade_module,
        "_global_facade",
        LoggingFacade(sinks=[sink]),
    )
    root = test_workspace / "snapshot-error-child-runtime"
    root.mkdir()
    project = await _open_ready_project(client, root)
    session = (
        await client.post(
            "/api/v1/sessions",
            json={"project_id": project["id"], "title": "Snapshot error child"},
        )
    ).json()

    class _SnapshotErrorProvider:
        name = "snapshot-error"

        def __init__(self, handlers: dict) -> None:
            self._handlers = handlers

        async def generate(self, _context):
            await self._handlers["patch_plan_nodes"](
                {
                    "add_nodes": [
                        {
                            "id": "snapshot-error-node",
                            "title": "Snapshot error node",
                            "goal": "Exercise a real incomplete execution snapshot",
                            "node_type": "code_change",
                            "files": ["src/missing.py"],
                            "tests": ["pytest tests/test_missing.py -q"],
                        }
                    ]
                }
            )
            await self._handlers["dispatch_child_agent"](
                {"node_id": "snapshot-error-node", "target_role": "executing"}
            )
            return AgentProposalSchema(summary="dispatched snapshot error")

    provider_calls = 0
    continued: list[str] = []

    def create_provider(context=None, **kwargs):
        nonlocal provider_calls
        assert context is not None
        provider_calls += 1
        if provider_calls == 1:
            return _SnapshotErrorProvider(kwargs["runtime_tool_handlers"])
        return _InstructionProvider(context.instruction, continued)

    monkeypatch.setattr(AgentProviderFactory, "create", staticmethod(create_provider))
    response = await client.post(
        f"/api/v1/sessions/{session['id']}/converse",
        json={"content": "dispatch real snapshot error"},
    )

    assert response.status_code == 201, response.text
    store = ProjectPlanStore(root, project_id=project["id"])
    assert store.get_node("snapshot-error-node")["status"] == "failed"
    with closing(sqlite3.connect(root / ".bridle" / "plan.db")) as connection:
        connection.row_factory = sqlite3.Row
        spawn = connection.execute(
            "SELECT status FROM child_spawn_facts WHERE node_id='snapshot-error-node'"
        ).fetchone()
        receipt = connection.execute(
            "SELECT result_status, result_json FROM child_result_receipts"
        ).fetchone()
    assert spawn is not None and spawn["status"] == "failed"
    assert receipt is not None and receipt["result_status"] == "failed"
    result_payload = json.loads(receipt["result_json"])
    assert result_payload["error_code"] == "module_boundary_incomplete"
    assert result_payload["detail"] == {
        "path": "src/missing.py",
        "reason": "missing_implementation_entity",
    }
    with closing(sqlite3.connect(root / ".bridle" / "mail.db")) as connection:
        mail_status = connection.execute(
            "SELECT status FROM mail_messages WHERE message_type='child-result'"
        ).fetchone()
    assert mail_status == ("delivered",)
    parent_handles = [
        handle
        for _, host, _ in gateway_module._runtime_components.values()
        for handle in host.active_handles()
        if handle.spec.session_id == session["id"]
        and handle.spec.role is gateway_module.RuntimeRole.PARENT
    ]
    assert len(parent_handles) == 1
    parent_runtime_id = parent_handles[0].spec.runtime_id
    failed_events = [
        event
        for event in sink.events
        if event.action == "runtime.state_changed"
        and event.detail.get("role") == "child"
        and event.detail.get("to_state") == "FAILED"
    ]
    assert len(failed_events) == 1

    continued_response = await client.post(
        f"/api/v1/sessions/{session['id']}/converse",
        json={"content": "continue after snapshot failure"},
    )
    assert continued_response.status_code == 201, continued_response.text
    assert continued_response.json()["content"] == "reply:continue after snapshot failure"
    assert continued == ["continue after snapshot failure"]
    continued_parents = [
        handle
        for _, host, _ in gateway_module._runtime_components.values()
        for handle in host.active_handles()
        if handle.spec.session_id == session["id"]
        and handle.spec.role is gateway_module.RuntimeRole.PARENT
    ]
    assert [handle.spec.runtime_id for handle in continued_parents] == [parent_runtime_id]
    await client.post(f"/api/v1/sessions/{session['id']}/close")


@pytest.mark.asyncio
@pytest.mark.parametrize("backpressure_status", ["full", "busy"])
async def test_close_active_gateway_session_cancels_children_and_preserves_history(
    client,
    test_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
    backpressure_status: str,
) -> None:
    root = test_workspace / "active-close-runtime"
    root.mkdir()
    project = await _open_ready_project(client, root)
    session = (
        await client.post(
            "/api/v1/sessions",
            json={"project_id": project["id"], "title": "Active close"},
        )
    ).json()
    dispatched = asyncio.Event()
    release = asyncio.Event()

    def create_provider(context=None, **kwargs):
        assert context is not None
        return _DispatchProvider(
            kwargs["runtime_tool_handlers"],
            ("child-cancelled",),
            dispatched=dispatched,
            release=release,
        )

    original_enqueue = gateway_module.PersistentMailbox.enqueue
    backpressure_attempts = 0

    def enqueue_with_child_result_backpressure(mailbox, envelope):
        nonlocal backpressure_attempts
        if envelope.message_type == "child-result":
            backpressure_attempts += 1
            return MailboxResult(
                status=backpressure_status,
                message_id=envelope.message_id,
            )
        return original_enqueue(mailbox, envelope)

    monkeypatch.setattr(AgentProviderFactory, "create", staticmethod(create_provider))
    monkeypatch.setattr(
        gateway_module.PersistentMailbox,
        "enqueue",
        enqueue_with_child_result_backpressure,
    )
    turn = asyncio.create_task(
        client.post(
            f"/api/v1/sessions/{session['id']}/converse",
            json={"content": "dispatch then wait"},
        )
    )
    await asyncio.wait_for(dispatched.wait(), timeout=2)
    closed = await asyncio.wait_for(
        client.post(f"/api/v1/sessions/{session['id']}/close"),
        timeout=5,
    )
    turn_result = (await asyncio.gather(turn, return_exceptions=True))[0]
    assert closed.status_code == 200
    assert closed.json()["status"] == "closed"
    assert isinstance(turn_result, BaseException) or turn_result.status_code >= 400
    history = await client.get(f"/api/v1/sessions/{session['id']}/messages")
    assert history.status_code == 200
    assert [message["role"] for message in history.json()] == ["user"]
    assert ProjectPlanStore(root, project_id=project["id"]).get_node("child-cancelled")[
        "status"
    ] == "failed"
    with closing(sqlite3.connect(root / ".bridle" / "plan.db")) as connection:
        receipt = connection.execute(
            "SELECT result_status, result_json FROM child_result_receipts"
        ).fetchone()
        spawn_status = connection.execute(
            "SELECT status FROM child_spawn_facts WHERE node_id = ?",
            ("child-cancelled",),
        ).fetchone()
    assert backpressure_attempts == 3
    assert spawn_status == ("cancelled",)
    assert receipt is not None
    assert receipt[0] == "cancelled"
    assert json.loads(receipt[1]) == {
        "error_code": "cancelled",
        "message": "Child runtime was cancelled",
    }
    assert all(
        handle.spec.session_id != session["id"]
        for _, host, _ in gateway_module._runtime_components.values()
        for handle in host.active_handles()
    )


@pytest.mark.asyncio
async def test_parent_mail_claim_waits_for_reverse_submit_and_preserves_sequence(
    client,
    db,
    test_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = test_workspace / "reverse-submit-runtime"
    root.mkdir()
    project = await _open_ready_project(client, root)
    session = (
        await client.post(
            "/api/v1/sessions",
            json={"project_id": project["id"], "title": "Reverse submit"},
        )
    ).json()
    execution_order: list[str] = []

    def create_provider(context=None, **_kwargs):
        assert context is not None
        return _InstructionProvider(context.instruction, execution_order)

    monkeypatch.setattr(AgentProviderFactory, "create", staticmethod(create_provider))
    bootstrap = await client.post(
        f"/api/v1/sessions/{session['id']}/converse",
        json={"content": "bootstrap"},
    )
    assert bootstrap.status_code == 201, bootstrap.text
    execution_order.clear()
    parent_handles = [
        handle
        for _, host, _ in gateway_module._runtime_components.values()
        for handle in host.active_handles()
        if handle.spec.session_id == session["id"]
        and handle.spec.role is gateway_module.RuntimeRole.PARENT
    ]
    assert len(parent_handles) == 1
    worker = gateway_module._parent_workers[parent_handles[0].spec.runtime_id]
    first = await gateway_module.ProjectSessionService.create_runtime_input(
        db,
        session["id"],
        content="first",
        target=worker.address,
        trace_id="reverse-first",
    )
    second = await gateway_module.ProjectSessionService.create_runtime_input(
        db,
        session["id"],
        content="second",
        target=worker.address,
        trace_id="reverse-second",
    )
    mailbox = gateway_module.PersistentMailbox(
        root / ".bridle" / "mail.db",
        project_id=project["id"],
        consumer_id=f"reverse-submit-{session['id']}",
        default_target=worker.address,
    )
    try:
        for message in (first, second):
            result = mailbox.enqueue(
                gateway_module.MailEnvelope(
                    message_id=message.id,
                    message_type="runtime-input",
                    source=gateway_module.AgentAddress(
                        project["id"],
                        "session-gateway",
                        1,
                    ),
                    target=worker.address,
                    payload={
                        "session_id": session["id"],
                        "session_message_id": message.id,
                    },
                )
            )
            assert result.status == "inserted"
    finally:
        await mailbox.close()
    with closing(sqlite3.connect(root / ".bridle" / "mail.db")) as connection:
        rows = [
            (row[0], row[1])
            for row in connection.execute(
                "SELECT message_id, sequence_no FROM mail_messages "
                "WHERE message_id IN (?, ?) ORDER BY sequence_no",
                (first.id, second.id),
            ).fetchall()
        ]
    assert [message_id for message_id, _ in rows] == [first.id, second.id]

    async def first_provider(_content: str) -> str:
        execution_order.append("first")
        return "reply:first"

    async def second_provider(_content: str) -> str:
        execution_order.append("second")
        return "reply:second"

    second_task = asyncio.create_task(
        worker.submit(
            sequence_no=rows[1][1],
            message_id=second.id,
            provider=second_provider,
            trace_id="reverse-second",
        )
    )
    await asyncio.sleep(0)
    assert not second_task.done()
    first_task = asyncio.create_task(
        worker.submit(
            sequence_no=rows[0][1],
            message_id=first.id,
            provider=first_provider,
            trace_id="reverse-first",
        )
    )
    responses = await asyncio.wait_for(
        asyncio.gather(first_task, second_task),
        timeout=5,
    )
    assert [response.content for response in responses] == ["reply:first", "reply:second"]
    with closing(sqlite3.connect(root / ".bridle" / "mail.db")) as connection:
        statuses = [
            row[0]
            for row in connection.execute(
                "SELECT status FROM mail_messages "
                "WHERE message_id IN (?, ?) ORDER BY sequence_no",
                (first.id, second.id),
            ).fetchall()
        ]
    assert execution_order == ["first", "second"]
    assert statuses == ["delivered", "delivered"]
    await client.post(f"/api/v1/sessions/{session['id']}/close")


@pytest.mark.asyncio
async def test_gateway_turn_logs_share_production_trace_across_parent_and_child(
    client,
    test_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _restart_gateway_for_test()
    sink = _EventSink()
    facade = LoggingFacade(sinks=[sink])
    monkeypatch.setattr(logging_facade_module, "_global_facade", facade)
    root = test_workspace / "gateway-trace-runtime"
    root.mkdir()
    project = await _open_ready_project(client, root)
    session = (
        await client.post(
            "/api/v1/sessions",
            json={"project_id": project["id"], "title": "Gateway trace"},
        )
    ).json()

    def create_provider(context=None, **kwargs):
        assert context is not None
        return _DispatchProvider(kwargs["runtime_tool_handlers"], ("trace-child",))

    monkeypatch.setattr(AgentProviderFactory, "create", staticmethod(create_provider))
    response = await client.post(
        f"/api/v1/sessions/{session['id']}/converse",
        json={"content": "trace this turn"},
    )
    assert response.status_code == 201, response.text
    by_action = {event.action: event for event in sink.events}
    actions = (
        "runtime_input.persisted",
        "runtime_input.delivered",
        "runtime_parent.input_handled",
        "runtime_child.result_delivered",
    )
    events = [by_action[action] for action in actions]
    assert len({event.trace_id for event in events}) == 1
    assert events[0].trace_id is not None
    assert len({event.message_id for event in events[:3]}) == 1
    assert all(event.project_id == project["id"] for event in events)
    assert all(event.agent_id and event.generation for event in events)
    assert all(event.detail["attempt"] >= 0 for event in events)
    await client.post(f"/api/v1/sessions/{session['id']}/close")


@pytest.mark.asyncio
async def test_restarted_parent_recovers_pending_child_result_from_prior_generation(
    client,
    test_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _restart_gateway_for_test()
    sink = _EventSink()
    monkeypatch.setattr(
        logging_facade_module,
        "_global_facade",
        LoggingFacade(sinks=[sink]),
    )
    root = test_workspace / "parent-restart-child-result"
    root.mkdir()
    project = await _open_ready_project(client, root)
    session = (
        await client.post(
            "/api/v1/sessions",
            json={"project_id": project["id"], "title": "Recover child result"},
        )
    ).json()

    def create_dispatch_provider(context=None, **kwargs):
        assert context is not None
        return _DispatchProvider(kwargs["runtime_tool_handlers"], ("restart-child",))

    monkeypatch.setattr(
        AgentProviderFactory,
        "create",
        staticmethod(create_dispatch_provider),
    )
    first = await client.post(
        f"/api/v1/sessions/{session['id']}/converse",
        json={"content": "dispatch before restart"},
    )
    assert first.status_code == 201, first.text
    with closing(sqlite3.connect(root / ".bridle" / "mail.db")) as connection:
        result_message_id = connection.execute(
            "SELECT message_id FROM mail_messages WHERE message_type='child-result'"
        ).fetchone()[0]
        connection.execute(
            "UPDATE mail_messages SET status='pending', attempt=0, "
            "next_retry_at=NULL, lease_owner=NULL, lease_token=NULL, lease_expires_at=NULL "
            "WHERE message_id=?",
            (result_message_id,),
        )
        connection.commit()
    with closing(sqlite3.connect(root / ".bridle" / "plan.db")) as connection:
        connection.execute(
            "DELETE FROM child_result_receipts WHERE message_id=?",
            (result_message_id,),
        )
        connection.execute(
            "UPDATE child_spawn_facts SET status='pending' WHERE node_id='restart-child'"
        )
        connection.execute(
            "UPDATE plan_nodes SET status='mapping' WHERE id='restart-child'"
        )
        connection.commit()
    await _restart_gateway_for_test()
    execution_order: list[str] = []

    def create_recovery_provider(context=None, **_kwargs):
        assert context is not None
        return _InstructionProvider(context.instruction, execution_order)

    monkeypatch.setattr(
        AgentProviderFactory,
        "create",
        staticmethod(create_recovery_provider),
    )
    recovered = await client.post(
        f"/api/v1/sessions/{session['id']}/converse",
        json={"content": "continue after restart"},
    )
    assert recovered.status_code == 201, recovered.text
    store = ProjectPlanStore(root, project_id=project["id"])
    assert store.get_node("restart-child")["status"] == "completed"
    with closing(sqlite3.connect(root / ".bridle" / "plan.db")) as connection:
        receipt = connection.execute(
            "SELECT result_status FROM child_result_receipts WHERE message_id=?",
            (result_message_id,),
        ).fetchone()
    with closing(sqlite3.connect(root / ".bridle" / "mail.db")) as connection:
        mail_status = connection.execute(
            "SELECT status FROM mail_messages WHERE message_id=?",
            (result_message_id,),
        ).fetchone()
    assert receipt == ("completed",)
    assert mail_status == ("delivered",)
    recovery_events = [
        event
        for event in sink.events
        if event.action == "runtime_child.result_recovered"
        and event.message_id == result_message_id
    ]
    assert len(recovery_events) == 1
    assert recovery_events[0].generation == 2
    await client.post(f"/api/v1/sessions/{session['id']}/close")


@pytest.mark.asyncio
async def test_restarted_parent_consumes_prior_generation_input_before_current_turn(
    client,
    db,
    test_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _restart_gateway_for_test()
    root = test_workspace / "parent-restart-runtime-input"
    root.mkdir()
    project = await _open_ready_project(client, root)
    session = (
        await client.post(
            "/api/v1/sessions",
            json={"project_id": project["id"], "title": "Recover runtime input"},
        )
    ).json()
    execution_order: list[str] = []

    def create_provider(context=None, **_kwargs):
        assert context is not None
        return _InstructionProvider(context.instruction, execution_order)

    monkeypatch.setattr(AgentProviderFactory, "create", staticmethod(create_provider))
    bootstrap = await client.post(
        f"/api/v1/sessions/{session['id']}/converse",
        json={"content": "bootstrap"},
    )
    assert bootstrap.status_code == 201, bootstrap.text
    await _restart_gateway_for_test()
    prior_target = gateway_module.AgentAddress(
        project["id"],
        f"session-{session['id']}",
        1,
    )
    prior_input = await gateway_module.ProjectSessionService.create_runtime_input(
        db,
        session["id"],
        content="recover me",
        target=prior_target,
        trace_id="prior-generation-input",
    )
    mailbox = gateway_module.PersistentMailbox(
        root / ".bridle" / "mail.db",
        project_id=project["id"],
        consumer_id=f"prior-generation-{session['id']}",
        default_target=prior_target,
    )
    try:
        enqueued = mailbox.enqueue(
            gateway_module.MailEnvelope(
                message_id=prior_input.id,
                message_type="runtime-input",
                source=gateway_module.AgentAddress(
                    project["id"],
                    "session-gateway",
                    1,
                ),
                target=prior_target,
                payload={
                    "session_id": session["id"],
                    "session_message_id": prior_input.id,
                },
            )
        )
        assert enqueued.status == "inserted"
    finally:
        await mailbox.close()
    execution_order.clear()

    recovered = await asyncio.wait_for(
        client.post(
            f"/api/v1/sessions/{session['id']}/converse",
            json={"content": "current turn"},
        ),
        timeout=5,
    )

    assert recovered.status_code == 201, recovered.text
    assert execution_order == ["recover me", "current turn"]
    history = (
        await client.get(f"/api/v1/sessions/{session['id']}/messages")
    ).json()
    assert [message["content"] for message in history[-4:]] == [
        "recover me",
        "current turn",
        "reply:recover me",
        "reply:current turn",
    ]
    await client.post(f"/api/v1/sessions/{session['id']}/close")


@pytest.mark.asyncio
@pytest.mark.parametrize("ack_failure_status", ["busy", "lost_lease"])
async def test_restarted_parent_survives_prior_generation_provider_failure(
    client,
    db,
    test_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
    ack_failure_status: str,
) -> None:
    await _restart_gateway_for_test()
    root = test_workspace / "parent-restart-provider-failure"
    root.mkdir()
    project = await _open_ready_project(client, root)
    session = (
        await client.post(
            "/api/v1/sessions",
            json={"project_id": project["id"], "title": "Failed runtime recovery"},
        )
    ).json()
    execution_order: list[str] = []

    class _FailedRecoveryProvider:
        name = "failed-recovery"

        async def generate(self, _context):
            raise RuntimeError("recovery_provider_exploded")

    def create_provider(context=None, **_kwargs):
        assert context is not None
        if context.instruction == "recover me":
            return _FailedRecoveryProvider()
        return _InstructionProvider(context.instruction, execution_order)

    monkeypatch.setattr(AgentProviderFactory, "create", staticmethod(create_provider))
    bootstrap = await client.post(
        f"/api/v1/sessions/{session['id']}/converse",
        json={"content": "bootstrap"},
    )
    assert bootstrap.status_code == 201, bootstrap.text
    await _restart_gateway_for_test()
    prior_target = gateway_module.AgentAddress(
        project["id"],
        f"session-{session['id']}",
        1,
    )
    prior_input = await gateway_module.ProjectSessionService.create_runtime_input(
        db,
        session["id"],
        content="recover me",
        target=prior_target,
        trace_id="failed-prior-generation-input",
    )
    mailbox = gateway_module.PersistentMailbox(
        root / ".bridle" / "mail.db",
        project_id=project["id"],
        consumer_id=f"failed-prior-generation-{session['id']}",
        default_target=prior_target,
    )
    try:
        enqueued = mailbox.enqueue(
            gateway_module.MailEnvelope(
                message_id=prior_input.id,
                message_type="runtime-input",
                source=gateway_module.AgentAddress(
                    project["id"],
                    "session-gateway",
                    1,
                ),
                target=prior_target,
                payload={
                    "session_id": session["id"],
                    "session_message_id": prior_input.id,
                },
            )
        )
        assert enqueued.status == "inserted"
    finally:
        await mailbox.close()
    execution_order.clear()
    original_ack = gateway_module.PersistentMailbox.ack
    failed_ack_attempts = 0

    def fail_prior_input_ack_once(mailbox, message_id, lease_token, *, target):
        nonlocal failed_ack_attempts
        if message_id == prior_input.id and failed_ack_attempts == 0:
            failed_ack_attempts += 1
            if ack_failure_status == "lost_lease":
                delivered = original_ack(mailbox, message_id, lease_token, target=target)
                assert delivered.status == "acked"
                return original_ack(mailbox, message_id, lease_token, target=target)
            return MailboxResult(status=ack_failure_status, message_id=message_id)
        return original_ack(mailbox, message_id, lease_token, target=target)

    monkeypatch.setattr(
        gateway_module.PersistentMailbox,
        "ack",
        fail_prior_input_ack_once,
    )

    continued = await asyncio.wait_for(
        client.post(
            f"/api/v1/sessions/{session['id']}/converse",
            json={"content": "current turn"},
        ),
        timeout=5,
    )

    assert continued.status_code == 201, continued.text
    assert continued.json()["content"] == "reply:current turn"
    assert execution_order == ["current turn"]
    assert failed_ack_attempts == 1
    with closing(sqlite3.connect(root / ".bridle" / "mail.db")) as connection:
        prior_status = connection.execute(
            "SELECT status FROM mail_messages WHERE message_id = ?",
            (prior_input.id,),
        ).fetchone()
    assert prior_status == ("delivered",)
    await client.post(f"/api/v1/sessions/{session['id']}/close")


@pytest.mark.asyncio
async def test_concurrent_first_gateway_turns_share_one_parent_runtime(
    client,
    db,
    test_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _restart_gateway_for_test()
    root = test_workspace / "concurrent-first-gateway"
    root.mkdir()
    project = await _open_ready_project(client, root)
    session = (
        await client.post(
            "/api/v1/sessions",
            json={"project_id": project["id"], "title": "Concurrent first turns"},
        )
    ).json()
    execution_order: list[str] = []

    def create_provider(context=None, **_kwargs):
        assert context is not None
        return _InstructionProvider(context.instruction, execution_order)

    monkeypatch.setattr(AgentProviderFactory, "create", staticmethod(create_provider))
    session_factory = gateway_module.async_sessionmaker(db.bind, expire_on_commit=False)
    async with session_factory() as first_db, session_factory() as second_db:
        first, second = await asyncio.wait_for(
            asyncio.gather(
                gateway_module.AgentGateway.converse(first_db, session["id"], "first"),
                gateway_module.AgentGateway.converse(second_db, session["id"], "second"),
            ),
            timeout=5,
        )

    assert {first.content, second.content} == {"reply:first", "reply:second"}
    parent_handles = [
        handle
        for _, host, _ in gateway_module._runtime_components.values()
        for handle in host.active_handles()
        if handle.spec.session_id == session["id"]
        and handle.spec.role is gateway_module.RuntimeRole.PARENT
    ]
    assert len(parent_handles) == 1
    assert sorted(execution_order) == ["first", "second"]
    await client.post(f"/api/v1/sessions/{session['id']}/close")


@pytest.mark.asyncio
@pytest.mark.parametrize("ack_failure_mode", ["busy_exhausted", "lost_lease"])
async def test_successful_provider_ack_failure_settles_once(
    client,
    test_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
    ack_failure_mode: str,
) -> None:
    await _restart_gateway_for_test()
    root = test_workspace / f"successful-provider-{ack_failure_mode}"
    root.mkdir()
    project = await _open_ready_project(client, root)
    session = (
        await client.post(
            "/api/v1/sessions",
            json={"project_id": project["id"], "title": "ACK recovery"},
        )
    ).json()
    execution_order: list[str] = []

    def create_provider(context=None, **_kwargs):
        assert context is not None
        return _InstructionProvider(context.instruction, execution_order)

    monkeypatch.setattr(AgentProviderFactory, "create", staticmethod(create_provider))
    original_ack = gateway_module.PersistentMailbox.ack
    original_nack = gateway_module.PersistentMailbox.nack
    failed_ack_attempts = 0
    acquire_lock = threading.Event()
    lock_ready = threading.Event()
    release_lock = threading.Event()
    lock_released = threading.Event()

    def hold_mail_write_lock() -> None:
        acquire_lock.wait(timeout=2)
        with closing(sqlite3.connect(root / ".bridle" / "mail.db")) as blocker:
            blocker.execute("BEGIN IMMEDIATE")
            lock_ready.set()
            release_lock.wait(timeout=2)
            blocker.rollback()
        lock_released.set()

    lock_thread = None
    if ack_failure_mode == "busy_exhausted":
        lock_thread = threading.Thread(target=hold_mail_write_lock, daemon=True)
        lock_thread.start()

    def fail_ack_with_real_mailbox_state(mailbox, message_id, lease_token, *, target):
        nonlocal failed_ack_attempts
        if ack_failure_mode == "busy_exhausted" and failed_ack_attempts < 3:
            if failed_ack_attempts == 0:
                acquire_lock.set()
                assert lock_ready.wait(timeout=1)
            failed_ack_attempts += 1
            result = original_ack(mailbox, message_id, lease_token, target=target)
            assert result.status == "mailbox_busy"
            if failed_ack_attempts == 3:
                release_lock.set()
                assert lock_released.wait(timeout=1)
            return result
        if ack_failure_mode == "lost_lease" and failed_ack_attempts == 0:
            failed_ack_attempts += 1
            released = original_nack(mailbox, message_id, lease_token, target=target)
            assert released.status == "nacked"
            return original_ack(mailbox, message_id, lease_token, target=target)
        return original_ack(mailbox, message_id, lease_token, target=target)

    monkeypatch.setattr(
        gateway_module.PersistentMailbox,
        "ack",
        fail_ack_with_real_mailbox_state,
    )
    first = await asyncio.wait_for(
        client.post(
            f"/api/v1/sessions/{session['id']}/converse",
            json={"content": "first"},
        ),
        timeout=5,
    )
    assert first.status_code == 201, first.text
    assert first.json()["content"] == "reply:first"
    if lock_thread is not None:
        lock_thread.join(timeout=1)
        assert not lock_thread.is_alive()
    parent_runtime_id = next(
        handle.spec.runtime_id
        for _, host, _ in gateway_module._runtime_components.values()
        for handle in host.active_handles()
        if handle.spec.session_id == session["id"]
        and handle.spec.role is gateway_module.RuntimeRole.PARENT
    )
    if ack_failure_mode == "lost_lease":
        await asyncio.sleep(1.1)
    second = await asyncio.wait_for(
        client.post(
            f"/api/v1/sessions/{session['id']}/converse",
            json={"content": "second"},
        ),
        timeout=5,
    )
    assert second.status_code == 201, second.text
    assert second.json()["content"] == "reply:second"
    assert execution_order == ["first", "second"]
    assert failed_ack_attempts == (3 if ack_failure_mode == "busy_exhausted" else 1)
    continued_parents = [
        handle.spec.runtime_id
        for _, host, _ in gateway_module._runtime_components.values()
        for handle in host.active_handles()
        if handle.spec.session_id == session["id"]
        and handle.spec.role is gateway_module.RuntimeRole.PARENT
    ]
    assert continued_parents == [parent_runtime_id]
    with closing(sqlite3.connect(root / ".bridle" / "mail.db")) as connection:
        input_statuses = connection.execute(
            "SELECT status FROM mail_messages "
            "WHERE message_type = 'runtime-input' ORDER BY sequence_no"
        ).fetchall()
    assert input_statuses == [("delivered",), ("delivered",)]
    await client.post(f"/api/v1/sessions/{session['id']}/close")


@pytest.mark.asyncio
@pytest.mark.parametrize("ack_failure_status", ["busy", "lost_lease"])
async def test_executing_provider_error_does_not_persist_candidate_result(
    client,
    test_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
    ack_failure_status: str,
) -> None:
    root = test_workspace / "provider-error-runtime"
    root.mkdir()
    example = root / "src" / "example.py"
    example.parent.mkdir(parents=True, exist_ok=True)
    example.write_text("# example\n", encoding="utf-8")
    (root / "tests" / "test_example.py").parent.mkdir(parents=True, exist_ok=True)
    (root / "tests" / "test_example.py").write_text("def test_example():\n    assert True\n", encoding="utf-8")

    project = await _open_ready_project(client, root)
    session = (await client.post("/api/v1/sessions", json={"project_id": project["id"]})).json()
    captured: list = []

    class _BoomProvider:
        name = "boom"

        async def generate(self, context):
            raise RuntimeError("provider_exploded")

    class _SuccessProvider:
        name = "success"

        async def generate(self, _context):
            return AgentProposalSchema(summary="reply-after-error")

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
                        }, {
                            "id": "continued-node",
                            "title": "Continued node",
                            "goal": "Continue after provider failure",
                            "node_type": "code_change",
                            "files": ["src/example.py"],
                            "tests": ["pytest tests/test_example.py -q"],
                        }],
                    })
                    return AgentProposalSchema(summary="reply-1")

            return _PlanOnce()
        if len(captured) == 2:
            return _BoomProvider()
        return _SuccessProvider()

    monkeypatch.setattr(
        "bridle.agent.providers.agent_provider.AgentProviderFactory.create",
        staticmethod(create_provider),
    )

    await client.post(f"/api/v1/sessions/{session['id']}/converse", json={"content": "plan"})
    parent_runtime_id = next(
        handle.spec.runtime_id
        for _, host, _ in gateway_module._runtime_components.values()
        for handle in host.active_handles()
        if handle.spec.session_id == session["id"]
        and handle.spec.role is gateway_module.RuntimeRole.PARENT
    )
    await client.post(
        f"/api/v1/sessions/{session['id']}/role",
        json={"role": "executing", "actor": "user", "confirmed": True},
    )
    original_ack = gateway_module.PersistentMailbox.ack
    original_nack = gateway_module.PersistentMailbox.nack
    failed_ack_attempts = 0

    def fail_runtime_input_ack_once(mailbox, message_id, lease_token, *, target):
        nonlocal failed_ack_attempts
        if failed_ack_attempts == 0:
            failed_ack_attempts += 1
            if ack_failure_status == "lost_lease":
                released = original_nack(mailbox, message_id, lease_token, target=target)
                assert released.status == "nacked"
                return original_ack(mailbox, message_id, lease_token, target=target)
            return MailboxResult(status=ack_failure_status, message_id=message_id)
        return original_ack(mailbox, message_id, lease_token, target=target)

    monkeypatch.setattr(
        gateway_module.PersistentMailbox,
        "ack",
        fail_runtime_input_ack_once,
    )
    with pytest.raises(RuntimeError, match="provider_exploded"):
        await asyncio.wait_for(
            client.post(
                f"/api/v1/sessions/{session['id']}/converse",
                json={"content": "execute", "node_id": "planned-node"},
            ),
            timeout=5,
        )
    candidate_roots = [
        path
        for path in (root / ".bridle" / "runtime" / "modules").rglob("candidates/*")
        if path.is_dir()
    ]
    assert candidate_roots
    assert all(not (path / "result.json").exists() for path in candidate_roots)
    assert failed_ack_attempts == 1
    await asyncio.sleep(1.1)
    continued = await asyncio.wait_for(
        client.post(
            f"/api/v1/sessions/{session['id']}/converse",
            json={"content": "continue", "node_id": "continued-node"},
        ),
        timeout=5,
    )
    assert continued.status_code == 201, continued.text
    assert continued.json()["content"] == "reply-after-error"
    continued_parents = [
        handle.spec.runtime_id
        for _, host, _ in gateway_module._runtime_components.values()
        for handle in host.active_handles()
        if handle.spec.session_id == session["id"]
        and handle.spec.role is gateway_module.RuntimeRole.PARENT
    ]
    assert continued_parents == [parent_runtime_id]
    with closing(sqlite3.connect(root / ".bridle" / "mail.db")) as connection:
        input_statuses = connection.execute(
            "SELECT status FROM mail_messages "
            "WHERE message_type = 'runtime-input' ORDER BY sequence_no"
        ).fetchall()
    assert input_statuses == [("delivered",), ("delivered",), ("delivered",)]


@pytest.mark.asyncio
async def test_executing_conversation_requires_an_explicit_plan_node(
    client,
    test_workspace: Path,
) -> None:
    """Send an executing turn without a node; request input exits as a fail-closed rejection."""
    root = test_workspace / "execution-node-required"
    root.mkdir()
    project = await _open_ready_project(client, root)
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

