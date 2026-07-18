"""Context template builder -structured prompt assembly for agent providers."""
from __future__ import annotations

import json
from typing import Any

from bridle.agent.tools.registry import AgentToolRegistry
from bridle.agent.context.types import ChildAgentResult, ContextPayload
from bridle.logging.jsonl import log_event
from bridle.agent.runtime.schemas import AgentContext

_SYSTEM_TEMPLATE = (
    "You are a coding agent operating within a master-worker architecture. "
    "You are a node-level worker agent executing a specific plan node. "
    "You follow strict test-driven development (TDD). Your execution order is: "
    "(1) write the test in the candidate workspace; "
    "(2) yield to the workflow, which automatically runs authoritative RED verification; "
    "(3) after RED is confirmed, use run_command to implement and diagnose inside the candidate container; "
    "(4) submit the candidate, after which the workflow automatically runs authoritative final verification; "
    "(5) inspect returned evidence and either continue fixing or finish. "
    "Use run_command and normal Bash tools to edit files in the candidate workspace; do not emit or apply incremental patches. "
    "You may ONLY use the provided tools according to their descriptors and sandbox policy. "
    "Respect allowed_files (which already includes the matching test file path), allowlisted tests, and network policy boundaries. "
    "Do not claim you executed tools you did not call. "
    "Before giving the final JSON completion confirmation, verify: tests passed in the GREEN run, evidence is sufficient, and the task is genuinely complete. "
    "If tests still fail, evidence is insufficient, or you cannot confirm completion, you must call report_blocked (or continue fixing) and you must not claim completion. "
    "Do not repeat the same tool call with the same arguments after a failure. "
    "If a tool fails, change your approach: modify arguments, try a different tool, or report_blocked. "
    "When finished, respond with a single JSON object matching: "
    '{"summary": string, "tests_to_run": [string]}'
)


class ContextTemplateBuilder:
    def __init__(
        self,
        context: AgentContext,
        *,
        short_term_memory: list[dict[str, Any]] | None = None,
        tool_context: list[dict[str, Any]] | None = None,
        long_term_memory: dict[str, Any] | None = None,
        rag: dict[str, Any] | None = None,
        child_agent_results: list[dict[str, Any]] | None = None,
        run_id: str | None = None,
        node_id: str | None = None,
    ) -> None:
        self._context = context
        self._short_term_memory = short_term_memory or []
        if tool_context is not None:
            self._tool_context = tool_context
        else:
            self._tool_context = [d.model_dump() for d in AgentToolRegistry.tool_descriptors()]
        self._long_term_memory = long_term_memory or {}
        self._rag = rag or {}
        self._child_agent_results = child_agent_results or []
        self._run_id = run_id
        self._node_id = node_id

    def build_payload(self) -> ContextPayload:
        ctx = self._context
        filtered_results = [self._filter_child_result(r) for r in self._child_agent_results]
        return ContextPayload(
            instruction=ctx.instruction,
            node=ctx.node,
            allowed_files=ctx.allowed_files,
            tests=ctx.tests,
            metrics=ctx.metrics,
            constraints=ctx.constraints,
            review_checks=ctx.review_checks,
            expected_outputs=ctx.expected_outputs,
            accessible_context=ctx.accessible_context,
            tool_capabilities=ctx.tool_capabilities,
            short_term_memory=self._short_term_memory,
            tool_context=self._tool_context,
            long_term_memory=self._long_term_memory,
            rag=self._rag,
            child_agent_results=[r.model_dump() for r in filtered_results],
        )

    @staticmethod
    def _filter_child_result(raw: dict[str, Any]) -> ChildAgentResult:
        return ChildAgentResult(
            node_id=str(raw.get("node_id", "")),
            status=str(raw.get("status", "unknown")),
            result_summary=str(raw.get("result_summary", "")),
            test_summary=str(raw.get("test_summary", "")),
            metrics_summary=str(raw.get("metrics_summary", "")),
            evidence_refs=list(raw.get("evidence_refs") or []),
        )

    def build_messages(self) -> list[dict[str, Any]]:
        payload = self.build_payload()
        layer_count = 4
        if self._short_term_memory:
            layer_count += 1
        if self._tool_context:
            layer_count += 1
        if self._child_agent_results:
            layer_count += 1

        log_event(
            "context_template_built",
            "completed",
            run_id=self._run_id,
            node_id=self._node_id,
            detail={"layer_count": layer_count},
        )

        if self._tool_context:
            log_event(
                "tool_context_disclosed",
                "completed",
                run_id=self._run_id,
                node_id=self._node_id,
                detail={"tool_count": len(self._tool_context)},
            )

        if self._child_agent_results:
            log_event(
                "child_agent_results_attached",
                "completed",
                run_id=self._run_id,
                node_id=self._node_id,
                detail={"result_count": len(self._child_agent_results)},
            )

        return [
            {"role": "system", "content": _SYSTEM_TEMPLATE},
            {"role": "user", "content": json.dumps(payload.model_dump(), ensure_ascii=False, default=str)},
        ]

