from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from backend.tests.agent.runtime.test_agent_runtime_host import (
    _claimed_mailbox,
    _database,
    _grant,
)


@pytest.mark.asyncio
async def test_runtime_uses_immutable_generation_capability_view_without_per_call_authorize(
    test_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sqlalchemy import event

    from bridle.agent.runtime.agent_runtime import RuntimeRole
    from bridle.agent.runtime.authorization import AgentAuthorizationService
    from bridle.agent.runtime.host import AgentRuntimeHost
    from bridle.agent.safety.sandbox_policy import SandboxPolicy
    from bridle.agent.skills.registry import SkillDefinition, SkillRegistry
    from bridle.agent.tools.registry import AgentToolRegistry
    from bridle.agent.tools.sandboxed_executor import SandboxedToolExecutor

    engine, sessions = await _database(test_workspace)
    sql_calls: list[str] = []
    event.listen(
        engine.sync_engine,
        "before_cursor_execute",
        lambda _conn, _cursor, statement, _parameters, _context, _executemany: sql_calls.append(
            statement
        ),
    )

    async def allowed(arguments):
        return {"status": "completed", "content": {"value": arguments["value"]}}

    async def hidden(arguments):
        return {"status": "completed", "content": arguments}

    policy = SandboxPolicy.for_run(
        run_id="runtime-capability",
        node_id="node",
        workspace_root=test_workspace,
        allowed_files=[],
        node_tests=[],
    )
    tool_registry = AgentToolRegistry(
        SandboxedToolExecutor(policy),
        runtime_handlers={"read_project_map": allowed, "patch_plan_nodes": hidden},
    )
    skill_definitions = {
        "visible-skill": SkillDefinition(
            id="visible-skill",
            name="Visible",
            description="visible",
            when_to_use="test",
            submodules={},
            prompt_fragments=("visible prompt",),
        ),
        "hidden-skill": SkillDefinition(
            id="hidden-skill",
            name="Hidden",
            description="hidden",
            when_to_use="test",
            submodules={},
            prompt_fragments=("hidden prompt",),
        ),
    }
    skill_registry = SkillRegistry(skill_definitions)
    host = AgentRuntimeHost(sessions)
    handle = await host.create_runtime(
        role=RuntimeRole.PARENT,
        project_id="project-capability",
        session_id="session-1",
        agent_id="parent",
        generation=1,
        grant=_grant(
            "project-capability",
            tools=("read_project_map",),
            skills=("visible-skill",),
        ),
        tool_registry=tool_registry,
        skill_registry=skill_registry,
    )
    sql_calls.clear()
    monkeypatch.setattr(
        AgentAuthorizationService,
        "resolve",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("runtime capability call re-entered authorization")
        ),
    )
    assert handle.capabilities.list_tools() == ("read_project_map",)
    assert handle.capabilities.list_skills() == ("visible-skill",)
    assert handle.capabilities.tool_manifest() == ({"id": "read_project_map"},)
    assert handle.capabilities.skill_manifest() == ({"id": "visible-skill"},)
    assert handle.capabilities.prompt_fragments() == ("visible prompt",)
    result = await handle.capabilities.execute_tool("read_project_map", {"value": 7})
    assert result["content"]["value"] == 7
    assert handle.capabilities.execute_tool("patch_plan_nodes", {})["error_code"] == (
        "unknown_capability"
    )
    assert handle.capabilities.get_skill("missing")["error_code"] == "unknown_capability"
    tool_registry._runtime_handlers["select_node"] = hidden
    skill_registry._skills["visible-skill"] = SkillDefinition(
        id="visible-skill",
        name="Mutated",
        description="mutated",
        when_to_use="test",
        submodules={},
        prompt_fragments=("mutated",),
    )
    assert handle.capabilities.list_tools() == ("read_project_map",)
    assert handle.capabilities.prompt_fragments() == ("visible prompt",)
    assert sql_calls == []
    await engine.dispose()


@pytest.mark.asyncio
async def test_child_view_is_parent_subset_and_revocation_replaces_generation(
    test_workspace: Path,
) -> None:
    from bridle.agent.runtime.agent_runtime import RuntimeRole, RuntimeState
    from bridle.agent.runtime.authorization import (
        AgentAuthorizationService,
        AgentIdentity,
        AgentRole,
        BudgetGrant,
        ToolGrant,
    )
    from bridle.agent.runtime.host import AgentRuntimeHost
    from bridle.agent.runtime.persistence import get_runtime_record
    from bridle.agent.runtime.persistent_mailbox import PersistentMailbox

    engine, sessions = await _database(test_workspace)
    service = AgentAuthorizationService()
    parent_grant = _grant("project-revoke", tools=("one", "two"))
    child_grant = service.derive(
        parent_grant,
        identity=AgentIdentity(
            principal_id="child",
            role=AgentRole.IMPLEMENTER,
            project_id="project-revoke",
            session_id="session-1",
        ),
        resource_scopes=(),
        tool_grants=(ToolGrant("one"),),
        skill_grants=(),
        budget_grant=BudgetGrant(),
    )
    tools = {"one": lambda arguments: arguments, "two": lambda arguments: arguments}
    host = AgentRuntimeHost(sessions)
    parent_mailbox, parent_target = _claimed_mailbox(
        test_workspace,
        project_id="project-revoke",
        agent_id="parent",
        consumer_id="revoke-parent-owner",
    )
    child_mailbox, child_target = _claimed_mailbox(
        test_workspace,
        project_id="project-revoke",
        agent_id="child",
        consumer_id="revoke-child-owner",
    )
    parent_task_finished = asyncio.Event()
    child_task_finished = asyncio.Event()

    async def parent_runtime_task(_handle) -> None:
        try:
            await asyncio.Event().wait()
        finally:
            parent_task_finished.set()

    async def child_runtime_task(_handle) -> None:
        try:
            await asyncio.Event().wait()
        finally:
            child_task_finished.set()

    parent = await host.create_runtime(
        role=RuntimeRole.PARENT,
        project_id="project-revoke",
        session_id="session-1",
        agent_id="parent",
        generation=1,
        grant=parent_grant,
        tools=tools,
        task_factory=parent_runtime_task,
        mailbox=parent_mailbox,
    )
    child = await host.create_runtime(
        role=RuntimeRole.CHILD,
        project_id="project-revoke",
        session_id="session-1",
        agent_id="child",
        generation=1,
        parent=parent,
        grant=child_grant,
        tools=tools,
        task_factory=child_runtime_task,
        mailbox=child_mailbox,
    )
    assert child.capabilities.list_tools() == ("one",)
    with pytest.raises(Exception, match="scope_escalation"):
        await host.create_runtime(
            role=RuntimeRole.CHILD,
            project_id="project-revoke",
            session_id="session-1",
            agent_id="bad-child",
            generation=1,
            parent=child,
            grant=parent_grant,
            tools=tools,
        )

    await host.revoke(parent)
    await asyncio.wait_for(parent_task_finished.wait(), timeout=1)
    await asyncio.wait_for(child_task_finished.wait(), timeout=1)
    assert parent.state == child.state == RuntimeState.DESTROYED
    assert parent.task is not None and parent.task.cancelled()
    assert child.task is not None and child.task.cancelled()
    assert parent not in host.active_handles()
    assert child not in host.active_handles()
    async with sessions() as session:
        parent_record = await get_runtime_record(session, parent.spec.runtime_id)
        child_record = await get_runtime_record(session, child.spec.runtime_id)
        assert parent_record.status == RuntimeState.DESTROYED
        assert child_record.status == RuntimeState.DESTROYED
    parent_replacement = PersistentMailbox(
        parent_mailbox.database_path,
        project_id="project-revoke",
        consumer_id="revoke-parent-replacement",
        default_target=parent_target,
    )
    child_replacement = PersistentMailbox(
        child_mailbox.database_path,
        project_id="project-revoke",
        consumer_id="revoke-child-replacement",
        default_target=child_target,
    )
    assert parent_replacement.claim(parent_target).status == "claimed"
    assert child_replacement.claim(child_target).status == "claimed"
    await parent_replacement.close()
    await child_replacement.close()
    replacement = await host.create_runtime(
        role=RuntimeRole.PARENT,
        project_id="project-revoke",
        session_id="session-1",
        agent_id="parent",
        generation=2,
        grant=_grant("project-revoke", tools=("two",)),
        tools=tools,
    )
    assert replacement.spec.generation == 2
    assert replacement.capabilities.list_tools() == ("two",)
    await engine.dispose()
