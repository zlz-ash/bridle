"""Plan complexity negotiation via plan-mode LLM."""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

from bridle.engine.agent_provider import AgentProviderFactory
from bridle.engine.deepseek_client import DEEPSEEK_DEFAULT_BASE
from bridle.engine.node_complexity_policy import NodeComplexityValidation, validate_plan_nodes
from bridle.engine.openai_client import HttpOpenAICompatibleClient, LLMHttpError, OpenAICompatibleClient
from bridle.logging.jsonl import log_event
from bridle.schemas.complexity_negotiation import NegotiationDecision, validate_negotiation_decision
from bridle.schemas.node import NodeImportSchema

logger = logging.getLogger("bridle")

_JSON_FENCE_RE = re.compile(r"```json\s*([\s\S]*?)\s*```", re.IGNORECASE)
_NEGOTIATION_TIMEOUT_SECONDS = 60.0
_MAX_LLM_ATTEMPTS = 2
_IMPORT_MAX_ROUNDS = 3
_RUNTIME_MAX_ROUNDS = 1
_RUNTIME_CACHE_TTL_SECONDS = 60.0

_VALID_ACTIONS = frozenset({"merge", "expand", "split", "accept_as_is", "replan"})

_RUNTIME_NEGOTIATION_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}


def get_runtime_negotiation_cache(plan_id: str) -> dict[str, Any] | None:
    entry = _RUNTIME_NEGOTIATION_CACHE.get(plan_id)
    if entry is None:
        return None
    ts, payload = entry
    if time.monotonic() - ts > _RUNTIME_CACHE_TTL_SECONDS:
        _RUNTIME_NEGOTIATION_CACHE.pop(plan_id, None)
        return None
    return dict(payload)


def set_runtime_negotiation_cache(plan_id: str, payload: dict[str, Any]) -> None:
    _RUNTIME_NEGOTIATION_CACHE[plan_id] = (time.monotonic(), dict(payload))


def clear_runtime_negotiation_cache(plan_id: str | None = None) -> None:
    if plan_id is None:
        _RUNTIME_NEGOTIATION_CACHE.clear()
    else:
        _RUNTIME_NEGOTIATION_CACHE.pop(plan_id, None)


class NegotiationProtocolError(Exception):
    """LLM response could not be parsed into a valid NegotiationDecision."""


class NegotiationDecisionRejected(Exception):
    """Decision rejected by policy (e.g. accept_as_is without micro)."""

    def __init__(self, node_id: str, message: str) -> None:
        self.node_id = node_id
        self.message = message
        super().__init__(message)


class ReplanRequestedError(Exception):
    """AI requested a full replan instead of patching nodes."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class PlanComplexityFailedError(Exception):
    """Complexity negotiation exhausted without a compliant plan."""

    def __init__(
        self,
        *,
        last_validations: list[NodeComplexityValidation],
        rounds_used: int,
        failure_reason: str | None = None,
    ) -> None:
        self.last_validations = last_validations
        self.rounds_used = rounds_used
        self.failure_reason = failure_reason
        super().__init__(failure_reason or "plan_not_executable")


def _default_negotiation_client() -> OpenAICompatibleClient:
    cfg = AgentProviderFactory.get_config()
    api_key = cfg["api_key"]
    if not api_key:
        raise NegotiationProtocolError("Missing BRIDLE_AGENT_API_KEY for complexity negotiation")
    base_url = cfg["base_url"] or DEEPSEEK_DEFAULT_BASE
    return HttpOpenAICompatibleClient(api_key=api_key, base_url=base_url, proxy=cfg["proxy"])


def _extract_content(response: dict[str, Any]) -> str:
    choices = response.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    return str(message.get("content") or "")


def _parse_decision_json(text: str) -> NegotiationDecision:
    matches = list(_JSON_FENCE_RE.finditer(text))
    raw = matches[-1].group(1).strip() if matches else text.strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise NegotiationProtocolError(f"Invalid JSON: {exc}") from exc
    try:
        decision = validate_negotiation_decision(data)
    except Exception as exc:
        raise NegotiationProtocolError(str(exc)) from exc
    if decision.action not in _VALID_ACTIONS:
        raise NegotiationProtocolError(f"Unknown action: {decision.action}")
    return decision


def _node_by_id(nodes: list[NodeImportSchema]) -> dict[str, NodeImportSchema]:
    return {n.id: n for n in nodes}


def _summarize_nodes(nodes: list[NodeImportSchema]) -> list[dict[str, Any]]:
    return [
        {
            "id": n.id,
            "title": n.title,
            "node_type": n.node_type,
            "estimated_minutes": n.estimated_minutes,
            "files": n.files[:10],
            "depends_on": n.depends_on,
            "goal": (n.goal or "")[:200],
        }
        for n in nodes
    ]


def apply_negotiation_decision(
    nodes: list[NodeImportSchema],
    decision: NegotiationDecision,
) -> None:
    """Apply one negotiation decision to in-memory plan nodes."""
    if decision.action == "replan":
        reason = decision.replan.reason if decision.replan else "replan requested"
        raise ReplanRequestedError(reason)

    by_id = _node_by_id(nodes)

    if decision.action == "merge":
        payload = decision.merge
        if payload is None:
            raise NegotiationProtocolError("merge action missing merge payload")
        to_remove = set(payload.node_ids)
        sources = [by_id[nid] for nid in payload.node_ids if nid in by_id]
        if len(sources) < 2:
            raise NegotiationProtocolError("merge requires at least two existing nodes")
        merged_deps: set[str] = set(payload.merged_depends_on)
        for src in sources:
            merged_deps.update(src.depends_on)
        merged_deps -= to_remove
        new_id = payload.node_ids[0]
        template = sources[0]
        merged = template.model_copy(
            update={
                "id": new_id,
                "title": payload.new_title,
                "goal": payload.new_goal,
                "estimated_minutes": payload.new_estimated_minutes,
                "files": list(payload.merged_files) or list(
                    dict.fromkeys([p for s in sources for p in s.files])
                ),
                "depends_on": sorted(merged_deps),
            }
        )
        nodes[:] = [n for n in nodes if n.id not in to_remove]
        nodes.append(merged)
        return

    if decision.action == "expand":
        payload = decision.expand
        if payload is None:
            raise NegotiationProtocolError("expand action missing expand payload")
        node = by_id.get(payload.node_id)
        if node is None:
            raise NegotiationProtocolError(f"Unknown node_id: {payload.node_id}")
        idx = nodes.index(node)
        extra_files = [f for f in payload.additional_files if f not in node.files]
        update: dict[str, object] = {
            "goal": payload.new_goal,
            "acceptance_scope": payload.new_acceptance_scope,
            "estimated_minutes": payload.new_estimated_minutes,
            "files": list(node.files) + extra_files,
        }
        if payload.new_tests:
            update["tests"] = list(payload.new_tests)
        nodes[idx] = node.model_copy(update=update)
        return

    if decision.action == "split":
        payload = decision.split
        if payload is None:
            raise NegotiationProtocolError("split action missing split payload")
        node = by_id.get(payload.node_id)
        if node is None:
            raise NegotiationProtocolError(f"Unknown node_id: {payload.node_id}")
        nodes.remove(node)
        for child in payload.into:
            nodes.append(
                node.model_copy(
                    update={
                        "id": child.id,
                        "title": child.title,
                        "goal": child.goal,
                        "estimated_minutes": child.estimated_minutes,
                        "files": child.files,
                        "depends_on": child.depends_on,
                        "node_type": child.node_type,  # type: ignore[arg-type]
                        "tests": child.tests or node.tests,
                    }
                )
            )
        return

    if decision.action == "accept_as_is":
        payload = decision.accept_as_is
        if payload is None:
            raise NegotiationProtocolError("accept_as_is missing payload")
        node = by_id.get(payload.node_id)
        if node is None:
            raise NegotiationProtocolError(f"Unknown node_id: {payload.node_id}")
        if node.node_type != "micro":
            raise NegotiationDecisionRejected(
                payload.node_id,
                "accept_as_is requires node_type=micro on target node",
            )
        idx = nodes.index(node)
        metrics = dict(node.metrics) if isinstance(node.metrics, dict) else {}
        metrics["complexity"] = {
            "exempted": True,
            "reason": payload.reason,
        }
        nodes[idx] = node.model_copy(update={"metrics": metrics})
        return

    raise NegotiationProtocolError(f"Unhandled action: {decision.action}")


class ComplexityNegotiationService:
    def __init__(self, client: OpenAICompatibleClient) -> None:
        self._client = client

    @staticmethod
    def default() -> ComplexityNegotiationService:
        return ComplexityNegotiationService(_default_negotiation_client())

    async def negotiate(
        self,
        *,
        plan_nodes: list[NodeImportSchema],
        validation_issues: list[NodeComplexityValidation],
        round_index: int,
        max_rounds: int = _IMPORT_MAX_ROUNDS,
    ) -> NegotiationDecision:
        cfg = AgentProviderFactory.get_config()
        messages = self._build_messages(
            plan_nodes=plan_nodes,
            validation_issues=validation_issues,
            round_index=round_index,
            max_rounds=max_rounds,
        )
        last_error: Exception | None = None
        for attempt in range(_MAX_LLM_ATTEMPTS):
            try:
                raw = await self._client.chat_completion(
                    messages=messages,
                    model=cfg["model"],
                    tools=None,
                    timeout_seconds=_NEGOTIATION_TIMEOUT_SECONDS,
                )
                content = _extract_content(raw)
                decision = _parse_decision_json(content)
                self._validate_decision_policy(decision, plan_nodes)
                log_event(
                    "complexity_negotiation",
                    "completed",
                    detail={
                        "action": decision.action,
                        "round_index": round_index,
                        "attempt": attempt + 1,
                    },
                )
                return decision
            except NegotiationDecisionRejected:
                raise
            except (NegotiationProtocolError, LLMHttpError, TimeoutError) as exc:
                last_error = exc
                logger.warning(
                    "complexity_negotiation_attempt_failed",
                    extra={"detail": {"round": round_index, "attempt": attempt + 1, "error": str(exc)}},
                )
        raise NegotiationProtocolError(str(last_error or "negotiation failed"))

    def _validate_decision_policy(
        self,
        decision: NegotiationDecision,
        plan_nodes: list[NodeImportSchema],
    ) -> None:
        if decision.action != "accept_as_is" or decision.accept_as_is is None:
            return
        by_id = _node_by_id(plan_nodes)
        node = by_id.get(decision.accept_as_is.node_id)
        if node is None:
            raise NegotiationProtocolError(f"Unknown node_id: {decision.accept_as_is.node_id}")
        if node.node_type != "micro":
            raise NegotiationDecisionRejected(
                decision.accept_as_is.node_id,
                "accept_as_is requires node_type=micro; change node_type before exempting",
            )

    @staticmethod
    def _build_messages(
        *,
        plan_nodes: list[NodeImportSchema],
        validation_issues: list[NodeComplexityValidation],
        round_index: int,
        max_rounds: int,
    ) -> list[dict[str, str]]:
        schema_hint = {
            "action": "merge|expand|split|accept_as_is|replan",
            "merge": {"node_ids": [], "new_title": "", "new_goal": "", "new_estimated_minutes": 60, "merged_files": [], "merged_depends_on": []},
            "expand": {"node_id": "", "new_goal": "", "new_acceptance_scope": "", "new_estimated_minutes": 60, "additional_files": [], "new_tests": ["pytest tests/test_xxx.py -q"]},
            "split": {"node_id": "", "into": [{"id": "", "title": "", "goal": "", "estimated_minutes": 60, "files": []}]},
            "accept_as_is": {"node_id": "", "reason": ""},
            "replan": {"reason": ""},
        }
        issues_payload = [v.to_dict() for v in validation_issues if not v.ok]
        system = (
            "You adjust an execution plan so every node passes Bridle complexity rules. "
            f"Round {round_index + 1}/{max_rounds}. Return ONE ```json fence with NegotiationDecision. "
            "Default min estimated_minutes is 60. accept_as_is only if target node_type is micro. "
            "When failing_validations include node_incomplete:missing_tests, use expand with new_tests "
            "set to concrete pytest commands (e.g. pytest tests/test_module.py -q). "
            "When failing_validations include node_blocked entries, inspect blocked_by "
            "for the missing field (tests/metrics/review_checks/constraints) and use expand "
            "to set the corresponding field (new_tests for missing tests). "
            f"Schema hint: {json.dumps(schema_hint, ensure_ascii=False)}"
        )
        user = json.dumps(
            {
                "nodes": _summarize_nodes(plan_nodes),
                "failing_validations": issues_payload,
            },
            ensure_ascii=False,
        )
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]


async def run_import_complexity_negotiation(
    nodes: list[NodeImportSchema],
    *,
    negotiation_service: ComplexityNegotiationService | None = None,
    limits=None,
) -> list[NodeComplexityValidation]:
    """Negotiate until all nodes pass or raise PlanNotExecutableError / ReplanRequestedError."""
    validations = validate_plan_nodes(nodes, limits=limits)
    round_idx = 0
    failure_reason: str | None = None

    while round_idx < _IMPORT_MAX_ROUNDS:
        failing = [v for v in validations if not v.ok]
        if not failing:
            return validations
        svc = negotiation_service or ComplexityNegotiationService.default()
        try:
            decision = await svc.negotiate(
                plan_nodes=nodes,
                validation_issues=failing,
                round_index=round_idx,
                max_rounds=_IMPORT_MAX_ROUNDS,
            )
            apply_negotiation_decision(nodes, decision)
        except ReplanRequestedError as exc:
            raise ReplanRequestedError(exc.reason) from exc
        except NegotiationDecisionRejected as exc:
            failure_reason = exc.message
            synthetic = NodeComplexityValidation(
                node_id=exc.node_id,
                estimate=failing[0].estimate,
                ok=False,
                issues=[f"negotiation_rejected:{exc.message}"],
            )
            validations = [synthetic]
            round_idx += 1
            continue
        except NegotiationProtocolError as exc:
            failure_reason = str(exc)
            round_idx += 1
            validations = validate_plan_nodes(nodes, limits=limits)
            continue

        validations = validate_plan_nodes(nodes, limits=limits)
        log_event(
            "complexity_negotiation_applied",
            "completed",
            detail={"round_index": round_idx, "action": decision.action},
        )
        round_idx += 1

    failing = [v for v in validations if not v.ok]
    if failing:
        raise PlanComplexityFailedError(
            last_validations=failing,
            rounds_used=round_idx,
            failure_reason=failure_reason,
        )
    return validations
