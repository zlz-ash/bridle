"""Fail-closed authorization contracts for agent runtime grants."""

from __future__ import annotations

import hashlib
import json
import math
import posixpath
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from threading import Condition, Event, RLock
from typing import Any, Literal


class AgentRole(StrEnum):
    COORDINATOR = "coordinator"
    PROJECT_MAPPER = "project_mapper"
    IMPLEMENTER = "implementer"
    VERIFIER = "verifier"
    REVIEWER = "reviewer"


@dataclass(frozen=True)
class AgentIdentity:
    principal_id: str
    role: AgentRole
    project_id: str
    session_id: str | None = None
    run_id: str | None = None
    owner_id: str | None = None
    agent_id: str | None = None


@dataclass(frozen=True)
class ResourceScope:
    resource_type: str
    actions: frozenset[str]
    attributes: tuple[tuple[str, str], ...] = ()
    effect: Literal["allow", "deny"] = "allow"


@dataclass(frozen=True)
class ToolGrant:
    tool_id: str
    actions: frozenset[str] = frozenset({"execute"})


@dataclass(frozen=True)
class SkillGrant:
    skill_id: str
    submodules: frozenset[str] = frozenset()


@dataclass(frozen=True)
class BudgetGrant:
    max_provider_calls: int = 0
    max_tool_calls: int = 0
    max_wall_seconds: float = 0.0


@dataclass(frozen=True)
class AuthorizationDecision:
    _allowed: bool
    _reason: str
    decision_id: str
    revocation: RevocationToken | None = field(default=None, compare=False, repr=False)

    @property
    def allowed(self) -> bool:
        return self._allowed and not bool(self.revocation and self.revocation.revoked)

    @property
    def reason(self) -> str:
        if self._allowed and self.revocation and self.revocation.revoked:
            return "grant_revoked"
        return self._reason


@dataclass(frozen=True)
class AuthorizedContinuationResult[T]:
    decision: AuthorizationDecision
    executed: bool
    result: T | None = None


@dataclass(frozen=True)
class RevocationToken:
    """Thread-safe revocation flag with parent propagation."""

    parent: RevocationToken | None = None
    _event: Event = field(default_factory=Event, init=False, repr=False)
    _lock: Any = field(default_factory=RLock, init=False, repr=False)
    _pending_starts: list[int] = field(
        default_factory=lambda: [0],
        init=False,
        repr=False,
        compare=False,
    )
    _condition: Any = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_condition", Condition(self._lock))

    @property
    def revoked(self) -> bool:
        lineage = self._lineage()
        if lineage is None:
            return True
        return any(current._event.is_set() for current in lineage)

    def revoke(self) -> bool:
        with self._condition:
            changed = not self._event.is_set()
            self._event.set()
            while self._pending_starts[0] > 0:
                self._condition.wait()
            return changed

    def child(self) -> RevocationToken:
        return RevocationToken(parent=self)

    @contextmanager
    def active_guard(self) -> Iterator[bool]:
        """Linearize one authorization decision or pending-start reservation."""
        lineage = self._lineage()
        if lineage is None:
            yield False
            return

        for token in lineage:
            token._lock.acquire()
        try:
            yield not any(token._event.is_set() for token in lineage)
        finally:
            for token in reversed(lineage):
                token._lock.release()

    def _lineage(self) -> tuple[RevocationToken, ...] | None:
        lineage: list[RevocationToken] = []
        seen: set[int] = set()
        current: RevocationToken | None = self
        while current is not None:
            marker = id(current)
            if marker in seen:
                return None
            seen.add(marker)
            lineage.append(current)
            current = current.parent
        lineage.reverse()
        return tuple(lineage)

    def _reserve_start_locked(self) -> ContinuationStartReservation | None:
        lineage = self._lineage()
        if lineage is None or any(token._event.is_set() for token in lineage):
            return None
        for token in lineage:
            token._pending_starts[0] += 1
        return ContinuationStartReservation(lineage)


@dataclass
class ContinuationStartReservation:
    """A callback launch whose ``enter`` call is its logical start point."""

    lineage: tuple[RevocationToken, ...]
    _state: Literal["pending", "entered", "cancelled"] = "pending"

    def enter(self) -> None:
        self._finish("entered")

    def cancel(self) -> None:
        self._finish("cancelled")

    def _finish(self, state: Literal["entered", "cancelled"]) -> None:
        if self._state != "pending":
            return
        for token in self.lineage:
            token._lock.acquire()
        try:
            if self._state != "pending":
                return
            self._state = state
            for token in self.lineage:
                token._pending_starts[0] -= 1
                token._condition.notify_all()
        finally:
            for token in reversed(self.lineage):
                token._lock.release()


@dataclass(frozen=True)
class AgentGrant:
    policy_version: str
    identity: AgentIdentity
    resource_scopes: tuple[ResourceScope, ...] = ()
    tool_grants: tuple[ToolGrant, ...] = ()
    skill_grants: tuple[SkillGrant, ...] = ()
    budget_grant: BudgetGrant = BudgetGrant()
    grant_id: str = ""
    grant_hash: str = ""
    parent_grant_hash: str | None = None
    revocation: RevocationToken = field(default_factory=RevocationToken, compare=False)


class AgentAuthorizationService:
    """Build and evaluate immutable, fail-closed agent grants."""

    _KNOWN_RESOURCE_TYPES = frozenset(
        {
            "tool",
            "skill",
            "workspace_path",
            "candidate",
            "test_command",
            "map_seed",
            "map_revision",
            "plan_operation",
            "agent_run",
            "network",
            "provider_budget",
            "tool_budget",
            "time_budget",
        }
    )

    def resolve(
        self,
        *,
        identity: AgentIdentity,
        policy_version: str,
        resource_scopes: tuple[ResourceScope, ...] = (),
        tool_grants: tuple[ToolGrant, ...] = (),
        skill_grants: tuple[SkillGrant, ...] = (),
        budget_grant: BudgetGrant | None = None,
    ) -> AgentGrant:
        resolved_budget = budget_grant or BudgetGrant()
        return self._build_grant(
            policy_version=policy_version,
            identity=identity,
            resource_scopes=resource_scopes,
            tool_grants=tool_grants,
            skill_grants=skill_grants,
            budget_grant=resolved_budget,
            parent_grant_hash=None,
            revocation=RevocationToken(),
        )

    def authorize(
        self,
        grant: AgentGrant,
        *,
        action: str,
        resource_type: str,
        attributes: Mapping[str, str] | None = None,
    ) -> AuthorizationDecision:
        with grant.revocation.active_guard() as active:
            if not active:
                return self._decision(
                    grant,
                    allowed=False,
                    reason="grant_revoked",
                    action=action,
                    resource_type=resource_type,
                    attributes=attributes,
                )
            return self._authorize_active(
                grant,
                action=action,
                resource_type=resource_type,
                attributes=attributes,
            )

    def _authorize_active(
        self,
        grant: AgentGrant,
        *,
        action: str,
        resource_type: str,
        attributes: Mapping[str, str] | None,
    ) -> AuthorizationDecision:
        if resource_type not in self._KNOWN_RESOURCE_TYPES:
            return self._decision(
                grant,
                allowed=False,
                reason="unknown_resource",
                action=action,
                resource_type=resource_type,
                attributes=attributes,
            )

        request_attributes = self._normalize_attributes(attributes)
        matching = tuple(
            scope
            for scope in grant.resource_scopes
            if self._scope_matches_request(
                scope,
                action=action,
                resource_type=resource_type,
                request_attributes=request_attributes,
            )
        )
        if any(scope.effect == "deny" for scope in matching):
            return self._decision(
                grant,
                allowed=False,
                reason="explicit_deny",
                action=action,
                resource_type=resource_type,
                attributes=request_attributes,
            )
        if any(scope.effect == "allow" for scope in matching):
            return self._decision(
                grant,
                allowed=True,
                reason="allowed",
                action=action,
                resource_type=resource_type,
                attributes=request_attributes,
            )
        return self._decision(
            grant,
            allowed=False,
            reason="default_deny",
            action=action,
            resource_type=resource_type,
            attributes=request_attributes,
        )

    def derive(
        self,
        parent: AgentGrant,
        *,
        identity: AgentIdentity,
        resource_scopes: tuple[ResourceScope, ...],
        tool_grants: tuple[ToolGrant, ...],
        skill_grants: tuple[SkillGrant, ...],
        budget_grant: BudgetGrant,
    ) -> AgentGrant:
        if parent.revocation.revoked:
            raise ValueError("grant_revoked: cannot derive from a revoked grant")
        self._validate_identity(identity)
        if identity.project_id != parent.identity.project_id:
            raise ValueError("scope_escalation: child project_id differs from parent")
        if identity.session_id != parent.identity.session_id:
            raise ValueError("scope_escalation: child session_id differs from parent")

        normalized_scopes = self._normalize_resource_scopes(resource_scopes)
        normalized_tools = self._normalize_tool_grants(tool_grants)
        normalized_skills = self._normalize_skill_grants(skill_grants)
        normalized_budget = self._normalize_budget(budget_grant)

        parent_allows = tuple(
            scope for scope in parent.resource_scopes if scope.effect == "allow"
        )
        parent_denies = tuple(
            scope for scope in parent.resource_scopes if scope.effect == "deny"
        )
        for child_scope in normalized_scopes:
            if child_scope.effect == "deny":
                continue
            if not any(
                self._scope_contains(parent_scope, child_scope)
                for parent_scope in parent_allows
            ):
                raise ValueError("scope_escalation: resource scope exceeds parent")
            if any(
                self._scopes_overlap(parent_deny, child_scope)
                for parent_deny in parent_denies
            ):
                raise ValueError("scope_escalation: resource scope conflicts with parent deny")

        parent_tools = {grant.tool_id: grant for grant in parent.tool_grants}
        for child_tool in normalized_tools:
            parent_tool = parent_tools.get(child_tool.tool_id)
            if parent_tool is None or not child_tool.actions <= parent_tool.actions:
                raise ValueError("scope_escalation: tool grant exceeds parent")

        parent_skills = {grant.skill_id: grant for grant in parent.skill_grants}
        for child_skill in normalized_skills:
            parent_skill = parent_skills.get(child_skill.skill_id)
            if parent_skill is None or not child_skill.submodules <= parent_skill.submodules:
                raise ValueError("scope_escalation: skill grant exceeds parent")

        if not self._budget_contains(parent.budget_grant, normalized_budget):
            raise ValueError("scope_escalation: budget exceeds parent")

        return self._build_grant(
            policy_version=parent.policy_version,
            identity=identity,
            resource_scopes=normalized_scopes,
            tool_grants=normalized_tools,
            skill_grants=normalized_skills,
            budget_grant=normalized_budget,
            parent_grant_hash=parent.grant_hash,
            revocation=parent.revocation.child(),
        )

    def execute_authorized[T](
        self,
        grant: AgentGrant,
        *,
        action: str,
        resource_type: str,
        attributes: Mapping[str, str] | None,
        continuation: Callable[[], T],
    ) -> AuthorizedContinuationResult[T]:
        """Reserve a logical start under the lineage lock, then run the callback unlocked."""
        reservation: ContinuationStartReservation | None = None
        with grant.revocation.active_guard() as active:
            if not active:
                decision = self._decision(
                    grant,
                    allowed=False,
                    reason="grant_revoked",
                    action=action,
                    resource_type=resource_type,
                    attributes=attributes,
                )
                return AuthorizedContinuationResult(decision=decision, executed=False)

            decision = self._authorize_active(
                grant,
                action=action,
                resource_type=resource_type,
                attributes=attributes,
            )
            if not decision.allowed:
                return AuthorizedContinuationResult(decision=decision, executed=False)
            reservation = grant.revocation._reserve_start_locked()
            if reservation is None:
                revoked = self._decision(
                    grant,
                    allowed=False,
                    reason="grant_revoked",
                    action=action,
                    resource_type=resource_type,
                    attributes=attributes,
                )
                return AuthorizedContinuationResult(decision=revoked, executed=False)

        try:
            reservation.enter()
            result = continuation()
        except BaseException:
            reservation.cancel()
            raise
        return AuthorizedContinuationResult(
            decision=decision,
            executed=True,
            result=result,
        )

    def _build_grant(
        self,
        *,
        policy_version: str,
        identity: AgentIdentity,
        resource_scopes: tuple[ResourceScope, ...],
        tool_grants: tuple[ToolGrant, ...],
        skill_grants: tuple[SkillGrant, ...],
        budget_grant: BudgetGrant,
        parent_grant_hash: str | None,
        revocation: RevocationToken,
    ) -> AgentGrant:
        if not isinstance(policy_version, str) or not policy_version.strip():
            raise ValueError("invalid_policy_version: policy_version must be non-empty")
        self._validate_identity(identity)
        normalized_scopes = self._normalize_resource_scopes(resource_scopes)
        normalized_tools = self._normalize_tool_grants(tool_grants)
        normalized_skills = self._normalize_skill_grants(skill_grants)
        normalized_budget = self._normalize_budget(budget_grant)
        payload = {
            "policy_version": policy_version,
            "identity": {
                "principal_id": identity.principal_id,
                "role": identity.role.value,
                "project_id": identity.project_id,
                "session_id": identity.session_id,
                "run_id": identity.run_id,
                "owner_id": identity.owner_id,
                "agent_id": identity.agent_id,
            },
            "resource_scopes": [self._scope_payload(scope) for scope in normalized_scopes],
            "tool_grants": [
                {"tool_id": grant.tool_id, "actions": sorted(grant.actions)}
                for grant in normalized_tools
            ],
            "skill_grants": [
                {"skill_id": grant.skill_id, "submodules": sorted(grant.submodules)}
                for grant in normalized_skills
            ],
            "budget_grant": {
                "max_provider_calls": normalized_budget.max_provider_calls,
                "max_tool_calls": normalized_budget.max_tool_calls,
                "max_wall_seconds": normalized_budget.max_wall_seconds,
            },
            "parent_grant_hash": parent_grant_hash,
        }
        grant_hash = self._stable_hash(payload)
        return AgentGrant(
            policy_version=policy_version,
            identity=identity,
            resource_scopes=normalized_scopes,
            tool_grants=normalized_tools,
            skill_grants=normalized_skills,
            budget_grant=normalized_budget,
            grant_id=f"grant-{grant_hash[:20]}",
            grant_hash=grant_hash,
            parent_grant_hash=parent_grant_hash,
            revocation=revocation,
        )

    def _decision(
        self,
        grant: AgentGrant,
        *,
        allowed: bool,
        reason: str,
        action: str,
        resource_type: str,
        attributes: Mapping[str, str] | tuple[tuple[str, str], ...] | None,
    ) -> AuthorizationDecision:
        normalized_attributes = self._normalize_attributes(attributes)
        decision_id = self._stable_hash(
            {
                "grant_hash": grant.grant_hash,
                "allowed": allowed,
                "reason": reason,
                "action": action,
                "resource_type": resource_type,
                "attributes": normalized_attributes,
            }
        )
        return AuthorizationDecision(
            allowed,
            reason,
            f"decision-{decision_id[:20]}",
            grant.revocation,
        )

    def _normalize_resource_scopes(
        self,
        scopes: tuple[ResourceScope, ...],
    ) -> tuple[ResourceScope, ...]:
        normalized = {self._normalize_scope(scope) for scope in scopes}
        return tuple(sorted(normalized, key=self._scope_sort_key))

    def _normalize_scope(self, scope: ResourceScope) -> ResourceScope:
        if scope.effect not in {"allow", "deny"}:
            raise ValueError("invalid_effect: effect must be allow or deny")
        if scope.resource_type not in self._KNOWN_RESOURCE_TYPES:
            raise ValueError("unknown_resource: resource type is not registered")
        if not scope.actions or any(
            not isinstance(action, str) or not action.strip() for action in scope.actions
        ):
            raise ValueError("invalid_action: actions must be non-empty strings")

        keys: set[str] = set()
        attributes: list[tuple[str, str]] = []
        for key, value in scope.attributes:
            if not isinstance(key, str) or not key.strip() or not isinstance(value, str):
                raise ValueError("invalid_attribute: attribute keys and values must be strings")
            if key in keys:
                raise ValueError(f"duplicate_attribute: {key}")
            keys.add(key)
            normalized_value = self._normalize_relative_path(value) if key == "path" else value
            attributes.append((key, normalized_value))
        return ResourceScope(
            resource_type=scope.resource_type,
            actions=frozenset(action.strip() for action in scope.actions),
            attributes=tuple(sorted(attributes)),
            effect=scope.effect,
        )

    @staticmethod
    def _normalize_tool_grants(grants: tuple[ToolGrant, ...]) -> tuple[ToolGrant, ...]:
        normalized: set[ToolGrant] = set()
        for grant in grants:
            if not isinstance(grant.tool_id, str) or not grant.tool_id.strip():
                raise ValueError("invalid_tool_grant: tool_id must be non-empty")
            if not grant.actions or any(
                not isinstance(action, str) or not action.strip() for action in grant.actions
            ):
                raise ValueError("invalid_tool_grant: actions must be non-empty strings")
            normalized.add(
                ToolGrant(
                    grant.tool_id.strip(),
                    frozenset(action.strip() for action in grant.actions),
                )
            )
        return tuple(
            sorted(
                normalized,
                key=lambda grant: (grant.tool_id, tuple(sorted(grant.actions))),
            )
        )

    @staticmethod
    def _normalize_skill_grants(grants: tuple[SkillGrant, ...]) -> tuple[SkillGrant, ...]:
        normalized: set[SkillGrant] = set()
        for grant in grants:
            if not isinstance(grant.skill_id, str) or not grant.skill_id.strip():
                raise ValueError("invalid_skill_grant: skill_id must be non-empty")
            if any(
                not isinstance(submodule, str) or not submodule.strip()
                for submodule in grant.submodules
            ):
                raise ValueError("invalid_skill_grant: submodules must be non-empty strings")
            normalized.add(
                SkillGrant(
                    grant.skill_id.strip(),
                    frozenset(submodule.strip() for submodule in grant.submodules),
                )
            )
        return tuple(
            sorted(
                normalized,
                key=lambda grant: (grant.skill_id, tuple(sorted(grant.submodules))),
            )
        )

    @staticmethod
    def _normalize_budget(budget: BudgetGrant) -> BudgetGrant:
        if type(budget.max_provider_calls) is not int or budget.max_provider_calls < 0:
            raise ValueError("invalid_budget: max_provider_calls must be a non-negative int")
        if type(budget.max_tool_calls) is not int or budget.max_tool_calls < 0:
            raise ValueError("invalid_budget: max_tool_calls must be a non-negative int")
        wall = budget.max_wall_seconds
        if (
            isinstance(wall, bool)
            or not isinstance(wall, (int, float))
            or not math.isfinite(wall)
            or wall < 0
        ):
            raise ValueError("invalid_budget: max_wall_seconds must be finite and non-negative")
        return BudgetGrant(
            max_provider_calls=budget.max_provider_calls,
            max_tool_calls=budget.max_tool_calls,
            max_wall_seconds=float(wall),
        )

    @staticmethod
    def _validate_identity(identity: AgentIdentity) -> None:
        if not isinstance(identity.role, AgentRole):
            raise ValueError("invalid_identity: role must be an AgentRole")
        for name in ("principal_id", "project_id"):
            value = getattr(identity, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"invalid_identity: {name} must be non-empty")
        for name in ("session_id", "run_id", "owner_id", "agent_id"):
            value = getattr(identity, name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"invalid_identity: {name} must be non-empty when set")

    @staticmethod
    def _stable_hash(payload: object) -> str:
        serialized = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    @staticmethod
    def _normalize_attributes(
        attributes: Mapping[str, str] | tuple[tuple[str, str], ...] | None,
    ) -> tuple[tuple[str, str], ...]:
        if attributes is None:
            return ()
        items = attributes.items() if isinstance(attributes, Mapping) else attributes
        return tuple(sorted((str(key), str(value)) for key, value in items))

    @staticmethod
    def _scope_payload(scope: ResourceScope) -> dict[str, object]:
        return {
            "resource_type": scope.resource_type,
            "actions": sorted(scope.actions),
            "attributes": list(scope.attributes),
            "effect": scope.effect,
        }

    @classmethod
    def _scope_sort_key(cls, scope: ResourceScope) -> str:
        return json.dumps(
            cls._scope_payload(scope),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @classmethod
    def _scope_matches_request(
        cls,
        scope: ResourceScope,
        *,
        action: str,
        resource_type: str,
        request_attributes: tuple[tuple[str, str], ...],
    ) -> bool:
        if scope.resource_type != resource_type or action not in scope.actions:
            return False
        request = dict(request_attributes)
        for key, expected in scope.attributes:
            actual = request.get(key)
            if actual is None:
                return False
            if key == "path":
                if not cls._path_contains(expected, actual):
                    return False
            elif actual != expected:
                return False
        return True

    @classmethod
    def _scope_contains(cls, parent: ResourceScope, child: ResourceScope) -> bool:
        if parent.resource_type != child.resource_type:
            return False
        if not child.actions <= parent.actions:
            return False
        parent_attributes = dict(parent.attributes)
        child_attributes = dict(child.attributes)
        for key, parent_value in parent_attributes.items():
            child_value = child_attributes.get(key)
            if child_value is None:
                return False
            if key == "path":
                if not cls._path_contains(parent_value, child_value):
                    return False
            elif child_value != parent_value:
                return False
        return True

    @classmethod
    def _scopes_overlap(cls, left: ResourceScope, right: ResourceScope) -> bool:
        if left.resource_type != right.resource_type:
            return False
        if not left.actions & right.actions:
            return False
        left_attributes = dict(left.attributes)
        right_attributes = dict(right.attributes)
        for key in left_attributes.keys() & right_attributes.keys():
            left_value = left_attributes[key]
            right_value = right_attributes[key]
            if key == "path":
                if not (
                    cls._path_contains(left_value, right_value)
                    or cls._path_contains(right_value, left_value)
                ):
                    return False
            elif left_value != right_value:
                return False
        return True

    @staticmethod
    def _path_contains(parent: str, child: str) -> bool:
        try:
            parent_norm = AgentAuthorizationService._normalize_relative_path(parent)
            child_norm = AgentAuthorizationService._normalize_relative_path(child)
        except ValueError:
            return False
        if parent_norm == ".":
            return True
        return child_norm == parent_norm or child_norm.startswith(f"{parent_norm.rstrip('/')}/")

    @staticmethod
    def _normalize_relative_path(path: str) -> str:
        if not isinstance(path, str) or not path or "\x00" in path:
            raise ValueError("invalid_path: path must be a non-empty string")
        replaced = path.replace("\\", "/")
        first_segment = replaced.split("/", 1)[0]
        normalized = posixpath.normpath(replaced)
        if (
            normalized == ".."
            or normalized.startswith("../")
            or normalized.startswith("/")
            or first_segment.endswith(":")
        ):
            raise ValueError("invalid_path: path must remain workspace-relative")
        return normalized

    @staticmethod
    def _budget_contains(parent: BudgetGrant, child: BudgetGrant) -> bool:
        return (
            0 <= child.max_provider_calls <= parent.max_provider_calls
            and 0 <= child.max_tool_calls <= parent.max_tool_calls
            and 0.0 <= child.max_wall_seconds <= parent.max_wall_seconds
        )
