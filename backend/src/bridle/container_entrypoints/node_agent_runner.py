"""Node-agent execution inside a container (no ORM / API imports)."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from bridle.container_entrypoints.container_manifest import build_manifest
from bridle.container_entrypoints.node_agent_inputs import NodeAgentInputs
from bridle.engine.agent_provider import AgentProviderFactory
from bridle.engine.proposal_path_validator import ProposalPathValidator
from bridle.engine.proposal_test_validator import validate_proposal_tests_to_run
from bridle.engine.sandbox_policy import SandboxPolicy
from bridle.engine.sandboxed_tool_executor import SandboxedToolExecutor
from bridle.logging.jsonl import log_event
from bridle.schemas.proposal import AgentContext, AgentProposalSchema


class ContainerNodeAgentRunner:
    def __init__(
        self,
        *,
        inputs: NodeAgentInputs,
        workspace_write_root: Path,
        outputs_dir: Path,
        run_id: str,
        node_id: str,
    ) -> None:
        self.inputs = inputs
        self.workspace_write_root = workspace_write_root
        self.outputs_dir = outputs_dir
        self.run_id = run_id
        self.node_id = node_id
        self._trace_path = outputs_dir / "llm_trace.jsonl"
        self._run_root = workspace_write_root.parent.parent

    async def execute(self) -> dict[str, Any]:
        self.workspace_write_root.mkdir(parents=True, exist_ok=True)
        self.outputs_dir.mkdir(parents=True, exist_ok=True)
        ctx = self.inputs.agent_context
        try:
            proposal = self._load_test_mode_proposal()
            if proposal is None:
                proposal = await self._call_provider(ctx)
            else:
                self._append_trace({"event": "test_mode_proposal", "summary": proposal.summary})
            self._append_trace({"event": "proposal_ready", "summary": proposal.summary})
        except TimeoutError:
            return build_manifest(
                run_id=self.run_id,
                plan_node_id=self.inputs.plan_node_id,
                baseline_revision=self.inputs.baseline_revision,
                write_files=[],
                summary="provider timeout",
                test_results={"tests": []},
                status="failed",
                error_code="timeout",
                run_root=self._run_root,
            )
        except Exception as exc:
            return build_manifest(
                run_id=self.run_id,
                plan_node_id=self.inputs.plan_node_id,
                baseline_revision=self.inputs.baseline_revision,
                write_files=[],
                summary=str(exc)[:500],
                test_results={"tests": []},
                status="failed",
                error_code=type(exc).__name__,
                run_root=self._run_root,
            )

        await self._apply_patches(ctx, proposal)
        test_results = await self._run_tests(ctx, proposal)
        tests = test_results.get("tests") if isinstance(test_results, dict) else []
        if not isinstance(tests, list):
            tests = []
        normalized_tests = self._normalize_test_results(tests, test_results)
        failed = any(isinstance(t, dict) and t.get("status") == "failed" for t in normalized_tests)
        status = "failed" if failed else "completed"
        return build_manifest(
            run_id=self.run_id,
            plan_node_id=self.inputs.plan_node_id,
            baseline_revision=self.inputs.baseline_revision,
            write_files=list(ctx.allowed_files),
            summary=proposal.summary,
            test_results={"tests": normalized_tests},
            status=status,
            error_code="test_failed" if failed else None,
            run_root=self._run_root,
        )

    async def _call_provider(self, ctx: AgentContext) -> AgentProposalSchema:
        provider = AgentProviderFactory.create(context=ctx)
        log_event(
            "container_node_provider_called",
            "started",
            run_id=self.run_id,
            detail={"provider": provider.name},
        )
        start = time.monotonic()
        proposal = await provider.generate(ctx)
        self._append_trace(
            {
                "event": "provider_completed",
                "provider": provider.name,
                "duration_ms": int((time.monotonic() - start) * 1000),
                "summary": proposal.summary,
            }
        )
        if not proposal.summary.strip():
            raise ValueError("EmptySummary")
        file_patches = [fp.model_dump() for fp in proposal.file_patches]
        errors = ProposalPathValidator.validate(file_patches, ctx.allowed_files)
        if errors:
            raise ValueError("PathBoundaryError")
        snap = ctx.tool_capabilities.get("sandbox", {}) if ctx.tool_capabilities else {}
        cmd_errors = validate_proposal_tests_to_run(proposal, snap, ctx.tests)
        if cmd_errors:
            raise ValueError("CommandPolicyError")
        return proposal

    async def _apply_patches(self, ctx: AgentContext, proposal: AgentProposalSchema) -> None:
        policy = self._sandbox_policy(ctx)
        executor = SandboxedToolExecutor(policy)
        for patch in proposal.file_patches:
            await executor.propose_file_patch(patch.path, patch.diff, patch.change_type)

    async def _run_tests(self, ctx: AgentContext, proposal: AgentProposalSchema) -> dict[str, Any]:
        commands = [str(c).strip() for c in proposal.tests_to_run if str(c).strip()]
        if not commands:
            return {
                "status": "completed",
                "tests": [
                    {
                        "name": "no_tests",
                        "command": "echo skip",
                        "status": "passed",
                        "exit_code": 0,
                        "duration_ms": 0,
                    }
                ],
            }
        policy = self._sandbox_policy(ctx, network_allowed=True)
        executor = SandboxedToolExecutor(policy)
        return await executor.run_allowed_tests(commands)

    def _sandbox_policy(self, ctx: AgentContext, *, network_allowed: bool = False) -> SandboxPolicy:
        return SandboxPolicy.for_run(
            run_id=self.run_id,
            node_id=self.node_id,
            workspace_root=self.workspace_write_root,
            allowed_files=list(ctx.allowed_files),
            node_tests=list(ctx.tests),
            network_allowed=network_allowed,
        )

    def _normalize_test_results(self, tests: list[Any], raw: dict[str, Any]) -> list[dict[str, Any]]:
        log_ref = f".aicoding/container-workspaces/{self.run_id}/diagnostics/container.log"
        normalized: list[dict[str, Any]] = []
        for idx, item in enumerate(tests):
            if not isinstance(item, dict):
                continue
            normalized.append(
                {
                    "name": str(item.get("name") or f"test_{idx}"),
                    "command": str(item.get("command") or ""),
                    "status": str(item.get("status") or "failed"),
                    "exit_code": int(item.get("exit_code", 1)),
                    "duration_ms": int(item.get("duration_ms", 0)),
                    "log_ref": str(item.get("log_ref") or log_ref),
                }
            )
        if not normalized:
            status = "passed" if raw.get("status") == "completed" else "failed"
            normalized.append(
                {
                    "name": "aggregate",
                    "command": "run_allowed_tests",
                    "status": status,
                    "exit_code": 0 if status == "passed" else 1,
                    "duration_ms": 0,
                    "log_ref": log_ref,
                }
            )
        return normalized

    def _load_test_mode_proposal(self) -> AgentProposalSchema | None:
        path = self._run_root / "inputs" / "test_mode.json"
        if not path.exists():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        proposal_payload = payload.get("proposal") or payload
        return AgentProposalSchema.model_validate(proposal_payload)

    def _append_trace(self, payload: dict[str, Any]) -> None:
        self._trace_path.parent.mkdir(parents=True, exist_ok=True)
        with self._trace_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
