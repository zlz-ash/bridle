"""Shared runtime identity, lifecycle, resources, and frozen capabilities."""

from __future__ import annotations

import asyncio
import copy
import inspect
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from bridle.agent.runtime.authorization import AgentGrant
from bridle.agent.runtime.mailbox import AgentAddress

if TYPE_CHECKING:
    from bridle.agent.skills.registry import SkillRegistry
    from bridle.agent.tools.registry import AgentToolRegistry


class RuntimeError(RuntimeError):
    """Stable runtime error for callers and tests."""

    def __init__(self, error_code: str) -> None:
        super().__init__(error_code)
        self.error_code = error_code


class RuntimeRole(StrEnum):
    PARENT = "parent"
    CHILD = "child"
    MAP = "map"


class RuntimeState(StrEnum):
    CREATING = "CREATING"
    READY = "READY"
    RUNNING = "RUNNING"
    STOPPING = "STOPPING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    DESTROYED = "DESTROYED"
    INTERRUPTED = "INTERRUPTED"


ALLOWED_TRANSITIONS: Mapping[RuntimeState, frozenset[RuntimeState]] = {
    RuntimeState.CREATING: frozenset(
        {RuntimeState.READY, RuntimeState.FAILED, RuntimeState.CANCELLED}
    ),
    RuntimeState.READY: frozenset(
        {
            RuntimeState.RUNNING,
            RuntimeState.STOPPING,
            RuntimeState.FAILED,
            RuntimeState.CANCELLED,
        }
    ),
    RuntimeState.RUNNING: frozenset(
        {
            RuntimeState.STOPPING,
            RuntimeState.COMPLETED,
            RuntimeState.FAILED,
            RuntimeState.CANCELLED,
            RuntimeState.INTERRUPTED,
        }
    ),
    RuntimeState.STOPPING: frozenset(
        {
            RuntimeState.COMPLETED,
            RuntimeState.FAILED,
            RuntimeState.CANCELLED,
            RuntimeState.INTERRUPTED,
        }
    ),
    RuntimeState.COMPLETED: frozenset({RuntimeState.DESTROYED}),
    RuntimeState.FAILED: frozenset({RuntimeState.DESTROYED}),
    RuntimeState.CANCELLED: frozenset({RuntimeState.DESTROYED}),
    RuntimeState.INTERRUPTED: frozenset({RuntimeState.DESTROYED}),
    RuntimeState.DESTROYED: frozenset(),
}


@dataclass(frozen=True)
class RuntimeSpec:
    runtime_id: str
    project_id: str
    agent_id: str
    generation: int
    role: RuntimeRole
    session_id: str | None = None
    parent_runtime_id: str | None = None

    @property
    def address(self) -> AgentAddress:
        return AgentAddress(self.project_id, self.agent_id, self.generation)


UnknownCapabilityLogger = Callable[[str, str], None]


class CapabilityView:
    """One generation's immutable provider-visible tool and skill definitions."""

    def __init__(
        self,
        *,
        grant: AgentGrant,
        tools: Mapping[str, Callable[[dict[str, Any]], Any]] | None = None,
        skills: Mapping[str, Mapping[str, Any]] | None = None,
        tool_registry: AgentToolRegistry | None = None,
        skill_registry: SkillRegistry | None = None,
        parent: CapabilityView | None = None,
        unknown_logger: UnknownCapabilityLogger | None = None,
    ) -> None:
        source_tools = dict(tools or {})
        if tool_registry is not None:
            frozen_tool_registry = tool_registry.frozen_copy()
            source_tools.update(
                {
                    descriptor.name: self._registry_tool(
                        frozen_tool_registry,
                        descriptor.name,
                    )
                    for descriptor in frozen_tool_registry.available_tool_descriptors()
                }
            )
        source_skills = dict(skills or {})
        if skill_registry is not None:
            frozen_skill_registry = skill_registry.frozen_copy()
            source_skills.update(
                {
                    skill_id: self._registry_skill(frozen_skill_registry.get(skill_id))
                    for skill_id in frozen_skill_registry.list_ids()
                }
            )
        tool_ids = {item.tool_id for item in grant.tool_grants}
        skill_ids = {item.skill_id for item in grant.skill_grants}
        if parent is not None and (
            not tool_ids <= set(parent._tools) or not skill_ids <= set(parent._skills)
        ):
            raise ValueError("scope_escalation: child capability view exceeds parent")
        frozen_tools = {
            tool_id: source_tools[tool_id]
            for tool_id in sorted(tool_ids)
            if tool_id in source_tools
        }
        frozen_skills = {
            skill_id: copy.deepcopy(source_skills[skill_id])
            for skill_id in sorted(skill_ids)
            if skill_id in source_skills
        }
        self._tools = MappingProxyType(frozen_tools)
        self._skills = MappingProxyType(frozen_skills)
        self._unknown_logger = unknown_logger

    @staticmethod
    def _registry_tool(
        registry: AgentToolRegistry,
        tool_id: str,
    ) -> Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]:
        async def execute(arguments: dict[str, Any]) -> dict[str, Any]:
            return await registry.execute(
                tool_id,
                arguments,
                tool_call_id=f"runtime-{tool_id}",
            )

        return execute

    @staticmethod
    def _registry_skill(definition: Any) -> dict[str, Any]:
        prompt_fragments = tuple(getattr(definition, "prompt_fragments", ()))
        return {
            "manifest": {
                "id": str(definition.id),
                "name": str(definition.name),
                "description": str(definition.description),
                "when_to_use": str(definition.when_to_use),
            },
            "prompt": "\n".join(str(fragment) for fragment in prompt_fragments),
        }

    def list_tools(self) -> tuple[str, ...]:
        return tuple(self._tools)

    def list_skills(self) -> tuple[str, ...]:
        return tuple(self._skills)

    def tool_manifest(self) -> tuple[dict[str, str], ...]:
        return tuple({"id": tool_id} for tool_id in self._tools)

    def skill_manifest(self) -> tuple[dict[str, str], ...]:
        return tuple({"id": skill_id} for skill_id in self._skills)

    def prompt_fragments(self) -> tuple[str, ...]:
        return tuple(
            str(definition["prompt"])
            for definition in self._skills.values()
            if definition.get("prompt")
        )

    def execute_tool(self, tool_id: str, arguments: dict[str, Any]) -> Any:
        tool = self._tools.get(tool_id)
        if tool is None:
            return self._unknown("tool", tool_id)
        return tool(copy.deepcopy(arguments))

    def get_skill(self, skill_id: str) -> Mapping[str, Any] | dict[str, str]:
        definition = self._skills.get(skill_id)
        if definition is None:
            return self._unknown("skill", skill_id)
        return MappingProxyType(copy.deepcopy(dict(definition)))

    def _unknown(self, kind: str, capability_id: str) -> dict[str, str]:
        if self._unknown_logger is not None:
            self._unknown_logger(kind, capability_id)
        return {"status": "failed", "error_code": "unknown_capability"}


ResourceCloser = Callable[[], Awaitable[Any] | Any]


@dataclass
class RuntimeHandle:
    spec: RuntimeSpec
    state: RuntimeState
    status_reason: str | None
    grant: AgentGrant
    capabilities: CapabilityView
    task: asyncio.Task[Any] | None = None
    children: set[str] = field(default_factory=set)
    _resources: list[ResourceCloser] = field(default_factory=list, repr=False)
    _resources_closed: bool = field(default=False, repr=False)
    _resource_close_task: asyncio.Task[None] | None = field(default=None, repr=False)
    _transition_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    _stop_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    _stop_task: asyncio.Task[RuntimeHandle] | None = field(default=None, repr=False)
    _destroy_task: asyncio.Task[RuntimeHandle] | None = field(default=None, repr=False)

    def add_resource(self, closer: ResourceCloser) -> None:
        if self._resources_closed:
            raise RuntimeError("runtime_destroyed")
        self._resources.append(closer)

    async def close_resources(
        self,
        on_error: Callable[[Exception], None] | None = None,
    ) -> None:
        if self._resources_closed:
            return
        if self._resource_close_task is None:
            self._resource_close_task = asyncio.create_task(
                self._finish_close_resources(on_error)
            )
        await asyncio.shield(self._resource_close_task)

    async def _finish_close_resources(
        self,
        on_error: Callable[[Exception], None] | None,
    ) -> None:
        while self._resources:
            closer = self._resources[-1]
            try:
                result = closer()
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:
                if on_error is not None:
                    on_error(exc)
            finally:
                self._resources.pop()
        self._resources_closed = True
