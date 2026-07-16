from __future__ import annotations

from contextlib import contextmanager
from dataclasses import FrozenInstanceError, replace
from threading import Event, Thread

import pytest

from bridle.agent.runtime.authorization import (
    AgentAuthorizationService,
    AgentIdentity,
    AgentRole,
    BudgetGrant,
    ResourceScope,
    RevocationToken,
    SkillGrant,
    ToolGrant,
)


def _identity(
    principal_id: str = "agent-root",
    *,
    role: AgentRole = AgentRole.COORDINATOR,
) -> AgentIdentity:
    return AgentIdentity(
        principal_id=principal_id,
        role=role,
        project_id="project-1",
        session_id="session-1",
        run_id="run-1",
        owner_id="session-1",
        agent_id=principal_id,
    )


def _scope(
    resource_type: str,
    *actions: str,
    effect: str = "allow",
    **attributes: str,
) -> ResourceScope:
    return ResourceScope(
        resource_type=resource_type,
        actions=frozenset(actions),
        attributes=tuple(attributes.items()),
        effect=effect,  # type: ignore[arg-type]
    )


def test_unknown_resources_default_deny() -> None:
    service = AgentAuthorizationService()
    grant = service.resolve(identity=_identity(), policy_version="v1")

    unknown = service.authorize(grant, action="read", resource_type="unknown")
    undeclared = service.authorize(grant, action="write", resource_type="workspace_path")

    assert unknown.allowed is False
    assert unknown.reason == "unknown_resource"
    assert undeclared.allowed is False
    assert undeclared.reason == "default_deny"


def test_explicit_deny_overrides_allow() -> None:
    service = AgentAuthorizationService()
    grant = service.resolve(
        identity=_identity(),
        policy_version="v1",
        resource_scopes=(
            _scope("workspace_path", "read", path="src/a.py"),
            _scope("workspace_path", "read", effect="deny", path="src/a.py"),
        ),
    )

    decision = service.authorize(
        grant,
        action="read",
        resource_type="workspace_path",
        attributes={"path": "src/a.py"},
    )

    assert decision.allowed is False
    assert decision.reason == "explicit_deny"


def test_grant_hash_is_stable_for_normalized_input() -> None:
    service = AgentAuthorizationService()
    first = service.resolve(
        identity=_identity(),
        policy_version="v1",
        resource_scopes=(
            _scope("workspace_path", "read", path="src/a.py"),
            _scope("plan_operation", "read", operation="overview"),
        ),
        tool_grants=(ToolGrant("read_plan"), ToolGrant("read_code_map")),
        skill_grants=(SkillGrant("testing", frozenset({"testing.python"})),),
        budget_grant=BudgetGrant(1, 5, 30.0),
    )
    reordered = service.resolve(
        identity=_identity(),
        policy_version="v1",
        resource_scopes=tuple(reversed(first.resource_scopes)),
        tool_grants=tuple(reversed(first.tool_grants)),
        skill_grants=first.skill_grants,
        budget_grant=first.budget_grant,
    )
    version_changed = service.resolve(
        identity=_identity(),
        policy_version="v2",
        resource_scopes=first.resource_scopes,
        tool_grants=first.tool_grants,
        skill_grants=first.skill_grants,
        budget_grant=first.budget_grant,
    )

    assert first.grant_hash
    assert reordered.grant_hash == first.grant_hash
    assert version_changed.grant_hash != first.grant_hash


def test_subgrant_cannot_expand_any_scope() -> None:
    service = AgentAuthorizationService()
    parent = service.resolve(
        identity=_identity(),
        policy_version="v1",
        resource_scopes=(_scope("workspace_path", "read", "write", path="src"),),
        tool_grants=(ToolGrant("read_plan"), ToolGrant("propose_file_patch")),
        skill_grants=(
            SkillGrant("testing", frozenset({"testing.python", "testing.general"})),
        ),
        budget_grant=BudgetGrant(2, 10, 60.0),
    )
    child_identity = replace(
        _identity("agent-child", role=AgentRole.IMPLEMENTER),
        run_id="run-child",
    )

    child = service.derive(
        parent,
        identity=child_identity,
        resource_scopes=(_scope("workspace_path", "read", path="src/a.py"),),
        tool_grants=(ToolGrant("read_plan"),),
        skill_grants=(SkillGrant("testing", frozenset({"testing.python"})),),
        budget_grant=BudgetGrant(1, 4, 20.0),
    )

    assert child.identity == child_identity
    assert child.budget_grant.max_tool_calls == 4
    with pytest.raises(ValueError, match="scope_escalation"):
        service.derive(
            parent,
            identity=child_identity,
            resource_scopes=(_scope("workspace_path", "read", path="outside.py"),),
            tool_grants=(ToolGrant("run_allowed_tests"),),
            skill_grants=(SkillGrant("review", frozenset({"review.general"})),),
            budget_grant=BudgetGrant(3, 11, 61.0),
        )


def test_parent_revocation_blocks_existing_child_and_pending_continuation() -> None:
    service = AgentAuthorizationService()
    parent = service.resolve(
        identity=_identity(),
        policy_version="v1",
        resource_scopes=(_scope("plan_operation", "read", operation="overview"),),
        tool_grants=(ToolGrant("read_plan"),),
        budget_grant=BudgetGrant(1, 2, 10.0),
    )
    child = service.derive(
        parent,
        identity=replace(_identity("agent-child"), run_id="run-child"),
        resource_scopes=parent.resource_scopes,
        tool_grants=parent.tool_grants,
        skill_grants=(),
        budget_grant=BudgetGrant(1, 1, 5.0),
    )
    before = service.authorize(
        child,
        action="read",
        resource_type="plan_operation",
        attributes={"operation": "overview"},
    )
    assert before.allowed is True

    reached_barrier = Event()
    continue_after_revoke = Event()
    continuation_allowed: list[bool] = []

    def pending_continuation() -> None:
        reached_barrier.set()
        assert continue_after_revoke.wait(timeout=1)
        continuation_allowed.append(not child.revocation.revoked)

    thread = Thread(target=pending_continuation)
    thread.start()
    assert reached_barrier.wait(timeout=1)

    assert parent.revocation.revoke() is True
    assert parent.revocation.revoke() is False
    continue_after_revoke.set()
    thread.join(timeout=1)

    after = service.authorize(
        child,
        action="read",
        resource_type="plan_operation",
        attributes={"operation": "overview"},
    )
    assert continuation_allowed == [False]
    assert after.allowed is False
    assert after.reason == "grant_revoked"


def _broad_parent_and_narrow_child() -> tuple[
    AgentAuthorizationService,
    object,
    dict[str, object],
]:
    service = AgentAuthorizationService()
    parent = service.resolve(
        identity=_identity(),
        policy_version="v1",
        resource_scopes=(
            _scope("workspace_path", "read", "write", path="src"),
            _scope("candidate", "read", candidate_id="candidate-1"),
            _scope("map_seed", "resolve", seed_id="seed-1"),
            _scope("agent_run", "read", subject_run_id="subject-1"),
        ),
        tool_grants=(ToolGrant("read_plan", frozenset({"discover", "execute"})),),
        skill_grants=(
            SkillGrant("testing", frozenset({"testing.python", "testing.general"})),
        ),
        budget_grant=BudgetGrant(2, 10, 60.0),
    )
    child_arguments: dict[str, object] = {
        "identity": replace(
            _identity("agent-child", role=AgentRole.IMPLEMENTER),
            run_id="run-child",
        ),
        "resource_scopes": (
            _scope("workspace_path", "read", path="src/a.py"),
            _scope("candidate", "read", candidate_id="candidate-1"),
            _scope("map_seed", "resolve", seed_id="seed-1"),
            _scope("agent_run", "read", subject_run_id="subject-1"),
        ),
        "tool_grants": (ToolGrant("read_plan", frozenset({"execute"})),),
        "skill_grants": (SkillGrant("testing", frozenset({"testing.python"})),),
        "budget_grant": BudgetGrant(1, 4, 20.0),
    }
    return service, parent, child_arguments


@pytest.mark.parametrize(
    "dimension",
    [
        "project_id",
        "session_id",
        "resource_action",
        "workspace_path",
        "candidate",
        "map_seed",
        "subject_run",
        "tool_id",
        "tool_action",
        "skill_id",
        "skill_submodule",
        "provider_budget",
        "tool_budget",
        "time_budget",
    ],
)
def test_each_subgrant_escalation_dimension_is_rejected(dimension: str) -> None:
    service, parent, arguments = _broad_parent_and_narrow_child()
    identity = arguments["identity"]
    scopes = list(arguments["resource_scopes"])

    if dimension == "project_id":
        arguments["identity"] = replace(identity, project_id="project-2")
    elif dimension == "session_id":
        arguments["identity"] = replace(identity, session_id="session-2")
    elif dimension == "resource_action":
        scopes[0] = replace(scopes[0], actions=frozenset({"read", "execute"}))
        arguments["resource_scopes"] = tuple(scopes)
    elif dimension == "workspace_path":
        scopes[0] = replace(scopes[0], attributes=(("path", "outside.py"),))
        arguments["resource_scopes"] = tuple(scopes)
    elif dimension == "candidate":
        scopes[1] = replace(scopes[1], attributes=(("candidate_id", "candidate-2"),))
        arguments["resource_scopes"] = tuple(scopes)
    elif dimension == "map_seed":
        scopes[2] = replace(scopes[2], attributes=(("seed_id", "seed-2"),))
        arguments["resource_scopes"] = tuple(scopes)
    elif dimension == "subject_run":
        scopes[3] = replace(scopes[3], attributes=(("subject_run_id", "subject-2"),))
        arguments["resource_scopes"] = tuple(scopes)
    elif dimension == "tool_id":
        arguments["tool_grants"] = (ToolGrant("run_allowed_tests"),)
    elif dimension == "tool_action":
        arguments["tool_grants"] = (
            ToolGrant("read_plan", frozenset({"execute", "cancel"})),
        )
    elif dimension == "skill_id":
        arguments["skill_grants"] = (SkillGrant("review", frozenset()),)
    elif dimension == "skill_submodule":
        arguments["skill_grants"] = (
            SkillGrant("testing", frozenset({"testing.python", "testing.java"})),
        )
    elif dimension == "provider_budget":
        arguments["budget_grant"] = BudgetGrant(3, 4, 20.0)
    elif dimension == "tool_budget":
        arguments["budget_grant"] = BudgetGrant(1, 11, 20.0)
    elif dimension == "time_budget":
        arguments["budget_grant"] = BudgetGrant(1, 4, 61.0)

    with pytest.raises(ValueError, match="scope_escalation"):
        service.derive(parent, **arguments)  # type: ignore[arg-type]


def test_issued_grant_and_nested_scope_values_are_immutable() -> None:
    service, parent, arguments = _broad_parent_and_narrow_child()
    original_hash = parent.grant_hash
    child = service.derive(parent, **arguments)  # type: ignore[arg-type]

    with pytest.raises(FrozenInstanceError):
        parent.policy_version = "v2"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        parent.resource_scopes[0].effect = "deny"  # type: ignore[misc]
    with pytest.raises(AttributeError):
        parent.tool_grants[0].actions.add("cancel")  # type: ignore[attr-defined]

    assert parent.grant_hash == original_hash
    assert child.parent_grant_hash == original_hash


@pytest.mark.parametrize(
    "field_name",
    [
        "principal_id",
        "role",
        "project_id",
        "session_id",
        "run_id",
        "owner_id",
        "agent_id",
        "scope_effect",
        "resource_type",
        "scope_action",
        "scope_attribute",
        "tool_id",
        "tool_action",
        "skill_id",
        "skill_submodule",
        "provider_budget",
        "tool_budget",
        "time_budget",
    ],
)
def test_grant_hash_changes_for_each_effective_field(field_name: str) -> None:
    service = AgentAuthorizationService()
    identity = _identity()
    scope = _scope("workspace_path", "read", path="src/a.py")
    tool = ToolGrant("read_plan", frozenset({"execute"}))
    skill = SkillGrant("testing", frozenset({"testing.python"}))
    budget = BudgetGrant(1, 5, 30.0)
    baseline = service.resolve(
        identity=identity,
        policy_version="v1",
        resource_scopes=(scope,),
        tool_grants=(tool,),
        skill_grants=(skill,),
        budget_grant=budget,
    )

    if field_name == "principal_id":
        identity = replace(identity, principal_id="principal-2")
    elif field_name == "role":
        identity = replace(identity, role=AgentRole.REVIEWER)
    elif field_name == "project_id":
        identity = replace(identity, project_id="project-2")
    elif field_name == "session_id":
        identity = replace(identity, session_id="session-2")
    elif field_name == "run_id":
        identity = replace(identity, run_id="run-2")
    elif field_name == "owner_id":
        identity = replace(identity, owner_id="owner-2")
    elif field_name == "agent_id":
        identity = replace(identity, agent_id="agent-2")
    elif field_name == "scope_effect":
        scope = replace(scope, effect="deny")
    elif field_name == "resource_type":
        scope = replace(scope, resource_type="candidate")
    elif field_name == "scope_action":
        scope = replace(scope, actions=frozenset({"write"}))
    elif field_name == "scope_attribute":
        scope = replace(scope, attributes=(("path", "src/b.py"),))
    elif field_name == "tool_id":
        tool = replace(tool, tool_id="read_code_map")
    elif field_name == "tool_action":
        tool = replace(tool, actions=frozenset({"discover"}))
    elif field_name == "skill_id":
        skill = replace(skill, skill_id="review")
    elif field_name == "skill_submodule":
        skill = replace(skill, submodules=frozenset({"testing.general"}))
    elif field_name == "provider_budget":
        budget = replace(budget, max_provider_calls=2)
    elif field_name == "tool_budget":
        budget = replace(budget, max_tool_calls=6)
    elif field_name == "time_budget":
        budget = replace(budget, max_wall_seconds=31.0)

    variant = service.resolve(
        identity=identity,
        policy_version="v1",
        resource_scopes=(scope,),
        tool_grants=(tool,),
        skill_grants=(skill,),
        budget_grant=budget,
    )
    assert variant.grant_hash != baseline.grant_hash


def test_child_hash_changes_when_only_parent_grant_hash_changes() -> None:
    service, first_parent, arguments = _broad_parent_and_narrow_child()
    second_parent = service.resolve(
        identity=replace(first_parent.identity, owner_id="different-parent-owner"),
        policy_version=first_parent.policy_version,
        resource_scopes=first_parent.resource_scopes,
        tool_grants=first_parent.tool_grants,
        skill_grants=first_parent.skill_grants,
        budget_grant=first_parent.budget_grant,
    )

    first_child = service.derive(first_parent, **arguments)  # type: ignore[arg-type]
    second_child = service.derive(second_parent, **arguments)  # type: ignore[arg-type]

    assert first_child.identity == second_child.identity
    assert first_child.parent_grant_hash != second_child.parent_grant_hash
    assert first_child.grant_hash != second_child.grant_hash


def test_authorized_continuation_rechecks_revocation_before_handler() -> None:
    service, parent, arguments = _broad_parent_and_narrow_child()
    child = service.derive(parent, **arguments)  # type: ignore[arg-type]
    handler_calls: list[str] = []

    normal = service.execute_authorized(
        child,
        action="read",
        resource_type="candidate",
        attributes={"candidate_id": "candidate-1"},
        continuation=lambda: handler_calls.append("normal") or "ok",
    )
    assert normal.executed is True
    assert normal.result == "ok"

    reached_barrier = Event()
    continue_after_revoke = Event()
    results: list[object] = []

    def pending() -> None:
        reached_barrier.set()
        assert continue_after_revoke.wait(timeout=1)
        results.append(
            service.execute_authorized(
                child,
                action="read",
                resource_type="candidate",
                attributes={"candidate_id": "candidate-1"},
                continuation=lambda: handler_calls.append("late") or "unsafe",
            )
        )

    thread = Thread(target=pending)
    thread.start()
    assert reached_barrier.wait(timeout=1)
    assert parent.revocation.revoke() is True
    assert parent.revocation.revoke() is False
    continue_after_revoke.set()
    thread.join(timeout=1)

    late = results[0]
    assert late.executed is False
    assert late.decision.reason == "grant_revoked"
    assert handler_calls == ["normal"]


@pytest.mark.parametrize("attribute_name", ["candidate_id", "path"])
def test_duplicate_scope_attributes_are_rejected(attribute_name: str) -> None:
    service = AgentAuthorizationService()
    scope = ResourceScope(
        resource_type="candidate" if attribute_name == "candidate_id" else "workspace_path",
        actions=frozenset({"read"}),
        attributes=((attribute_name, "first"), (attribute_name, "second")),
    )

    with pytest.raises(ValueError, match="duplicate_attribute"):
        service.resolve(
            identity=_identity(),
            policy_version="v1",
            resource_scopes=(scope,),
        )


def test_revocation_lineage_is_immutable_and_parent_cannot_be_detached() -> None:
    service, parent, arguments = _broad_parent_and_narrow_child()
    child = service.derive(parent, **arguments)  # type: ignore[arg-type]

    with pytest.raises(FrozenInstanceError):
        child.revocation.parent = None  # type: ignore[misc]

    parent.revocation.revoke()
    assert child.revocation.revoked is True
    blocked = service.execute_authorized(
        child,
        action="read",
        resource_type="candidate",
        attributes={"candidate_id": "candidate-1"},
        continuation=lambda: "unsafe",
    )
    assert blocked.executed is False
    assert blocked.decision.reason == "grant_revoked"


def test_tampered_revocation_cycle_fails_closed() -> None:
    token = RevocationToken()
    object.__setattr__(token, "parent", token)

    assert token.revoked is True
    with token.active_guard() as active:
        assert active is False


def test_authorize_cannot_remain_allowed_after_concurrent_revoke() -> None:
    service, parent, arguments = _broad_parent_and_narrow_child()
    child = service.derive(parent, **arguments)  # type: ignore[arg-type]
    entered_match = Event()
    release_match = Event()
    revoke_started = Event()
    decisions: list[object] = []

    original_match = service._scope_matches_request

    def blocking_match(*args: object, **kwargs: object) -> bool:
        entered_match.set()
        assert release_match.wait(timeout=1)
        return original_match(*args, **kwargs)  # type: ignore[arg-type]

    service._scope_matches_request = blocking_match  # type: ignore[method-assign]

    authorize_thread = Thread(
        target=lambda: decisions.append(
            service.authorize(
                child,
                action="read",
                resource_type="candidate",
                attributes={"candidate_id": "candidate-1"},
            )
        )
    )
    authorize_thread.start()
    assert entered_match.wait(timeout=1)

    def revoke() -> None:
        revoke_started.set()
        parent.revocation.revoke()

    revoke_thread = Thread(target=revoke)
    revoke_thread.start()
    assert revoke_started.wait(timeout=1)
    release_match.set()
    authorize_thread.join(timeout=1)
    revoke_thread.join(timeout=1)

    decision = decisions[0]
    assert decision.allowed is False
    assert decision.reason == "grant_revoked"


def test_handler_can_wait_for_revoke_without_lineage_lock_deadlock() -> None:
    service, parent, arguments = _broad_parent_and_narrow_child()
    child = service.derive(parent, **arguments)  # type: ignore[arg-type]
    revoke_finished = Event()

    def handler() -> str:
        def revoke() -> None:
            parent.revocation.revoke()
            revoke_finished.set()

        thread = Thread(target=revoke)
        thread.start()
        assert revoke_finished.wait(timeout=1)
        thread.join(timeout=1)
        return "completed"

    result = service.execute_authorized(
        child,
        action="read",
        resource_type="candidate",
        attributes={"candidate_id": "candidate-1"},
        continuation=handler,
    )

    assert result.executed is True
    assert result.result == "completed"
    assert parent.revocation.revoked is True


@pytest.mark.parametrize(
    ("scope", "budget", "reason"),
    [
        (
            ResourceScope("workspace_path", frozenset({"read"}), effect="permit"),
            BudgetGrant(),
            "invalid_effect",
        ),
        (
            ResourceScope("unknown", frozenset({"read"})),
            BudgetGrant(),
            "unknown_resource",
        ),
        (
            ResourceScope(
                "candidate",
                frozenset({"read"}),
                (("candidate_id", "one"), ("candidate_id", "two")),
            ),
            BudgetGrant(),
            "duplicate_attribute",
        ),
        (ResourceScope("candidate", frozenset({"read"})), BudgetGrant(-1, 0, 0), "invalid_budget"),
        (ResourceScope("candidate", frozenset({"read"})), BudgetGrant(0, -1, 0), "invalid_budget"),
        (ResourceScope("candidate", frozenset({"read"})), BudgetGrant(0, 0, -1), "invalid_budget"),
        (ResourceScope("candidate", frozenset({"read"})), BudgetGrant(0, 0, float("nan")), "invalid_budget"),
        (ResourceScope("candidate", frozenset({"read"})), BudgetGrant(0, 0, float("inf")), "invalid_budget"),
        (ResourceScope("candidate", frozenset({"read"})), BudgetGrant(0, 0, float("-inf")), "invalid_budget"),
        (ResourceScope("candidate", frozenset({"read"})), BudgetGrant(True, 0, 0), "invalid_budget"),
        (ResourceScope("candidate", frozenset({"read"})), BudgetGrant(0, True, 0), "invalid_budget"),
        (ResourceScope("candidate", frozenset({"read"})), BudgetGrant(0, 0, True), "invalid_budget"),
    ],
)
def test_invalid_grant_inputs_fail_closed(
    scope: ResourceScope,
    budget: BudgetGrant,
    reason: str,
) -> None:
    service = AgentAuthorizationService()

    with pytest.raises(ValueError, match=reason):
        service.resolve(
            identity=_identity(),
            policy_version="v1",
            resource_scopes=(scope,),
            budget_grant=budget,
        )


def test_equivalent_paths_have_the_same_grant_hash() -> None:
    service = AgentAuthorizationService()
    hashes = {
        service.resolve(
            identity=_identity(),
            policy_version="v1",
            resource_scopes=(_scope("workspace_path", "read", path=path),),
        ).grant_hash
        for path in ("src/a.py", "src/./a.py", "src/x/../a.py", "src\\a.py")
    }

    assert len(hashes) == 1


@pytest.mark.parametrize("path", ["../outside.py", "src/../../outside.py", "C:\\outside.py"])
def test_unsafe_scope_paths_are_rejected(path: str) -> None:
    service = AgentAuthorizationService()

    with pytest.raises(ValueError, match="invalid_path"):
        service.resolve(
            identity=_identity(),
            policy_version="v1",
            resource_scopes=(_scope("workspace_path", "read", path=path),),
        )


def test_revoke_waits_for_pending_start_reservation(monkeypatch: pytest.MonkeyPatch) -> None:
    service, parent, arguments = _broad_parent_and_narrow_child()
    child = service.derive(parent, **arguments)  # type: ignore[arg-type]
    guard_released = Event()
    release_pending_start = Event()
    revoke_started = Event()
    revoke_finished = Event()
    handler_entered = Event()
    finish_handler = Event()
    results: list[object] = []
    original_guard = RevocationToken.active_guard

    @contextmanager
    def guarded(token: RevocationToken):
        with original_guard(token) as active:
            yield active
        if token is child.revocation:
            guard_released.set()
            assert release_pending_start.wait(timeout=1)

    monkeypatch.setattr(RevocationToken, "active_guard", guarded)

    def handler() -> str:
        handler_entered.set()
        assert finish_handler.wait(timeout=1)
        return "completed"

    execute_thread = Thread(
        target=lambda: results.append(
            service.execute_authorized(
                child,
                action="read",
                resource_type="candidate",
                attributes={"candidate_id": "candidate-1"},
                continuation=handler,
            )
        )
    )
    execute_thread.start()
    assert guard_released.wait(timeout=1)

    def revoke() -> None:
        revoke_started.set()
        parent.revocation.revoke()
        revoke_finished.set()

    revoke_thread = Thread(target=revoke)
    revoke_thread.start()
    assert revoke_started.wait(timeout=1)
    assert parent.revocation._event.wait(timeout=1)
    assert revoke_finished.is_set() is False

    release_pending_start.set()
    assert handler_entered.wait(timeout=1)
    assert revoke_finished.wait(timeout=1)
    assert finish_handler.is_set() is False

    finish_handler.set()
    execute_thread.join(timeout=1)
    revoke_thread.join(timeout=1)

    assert results[0].executed is True
    assert results[0].result == "completed"
