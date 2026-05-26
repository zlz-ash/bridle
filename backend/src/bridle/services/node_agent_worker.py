"""NodeAgentWorkerService — dispatch child model for a NodeAgentRun."""
from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bridle.config import get_config
from bridle.engine.agent_provider import AgentProviderFactory
from bridle.engine.container_workspace import ContainerWorkspaceBuilder
from bridle.engine.deepseek_agent_provider import DeepSeekProviderError
from bridle.engine.sandbox_policy import SandboxPolicy
from bridle.engine.proposal_path_validator import ProposalPathValidator
from bridle.engine.proposal_test_validator import validate_proposal_tests_to_run
from bridle.engine.sandboxed_tool_executor import SandboxedToolExecutor
from bridle.logging.jsonl import log_event
from bridle.models.node import NodeRecord
from bridle.models.node_agent_result import NodeAgentResultRecord
from bridle.models.node_agent_run import NodeAgentRunRecord
from bridle.models.proposal import ProposalRecord
from bridle.schemas.proposal import AgentContext, AgentProposalSchema
from bridle.schemas.node import unpack_container_boundary
from bridle.services.capability_policy import CapabilityPolicyService
from bridle.services.main_agent_container_service import MainAgentContainerService
from bridle.services.node_agent_run_service import NodeAgentRunService
from bridle.services.container_output_simulator import ContainerOutputSimulator
from bridle.services.node_container_orchestrator import NodeContainerError, NodeContainerOrchestrator
from bridle.engine.aggregate_plan import load_aggregate_strategies
from bridle.services.node_output_integration_service import NodeOutputIntegrationService
from bridle.services.node_service import NodeService
from bridle.utils.datetime_util import utc_now_naive

logger = logging.getLogger("bridle")

_test_db: AsyncSession | None = None


class NodeAgentWorkerService:
    @staticmethod
    def set_test_db(session: AsyncSession | None) -> None:
        global _test_db
        _test_db = session

    @staticmethod
    def start(run_id: str) -> None:
        """Enqueue in-process worker (uses test db hook when set)."""
        if _test_db is not None:
            asyncio.create_task(NodeAgentWorkerService.run_once(run_id, db=_test_db))
        else:
            asyncio.create_task(NodeAgentWorkerService._run_in_new_session(run_id))

    @staticmethod
    async def _run_in_new_session(run_id: str) -> None:
        from bridle.database import _ensure_engine, async_session

        _ensure_engine()
        async with async_session() as db:
            await NodeAgentWorkerService.run_once(run_id, db=db)

    @staticmethod
    async def run_once(run_id: str, *, db: AsyncSession) -> None:
        log_event("node_agent_worker_started", "started", run_id=run_id)
        result = await db.execute(
            select(NodeAgentRunRecord).where(NodeAgentRunRecord.id == run_id)
        )
        run = result.scalar_one_or_none()
        if run is None or run.status != "queued":
            return

        run.status = "running"
        run.phase = "planning"
        run.last_heartbeat_at = utc_now_naive()
        await db.commit()

        try:
            ctx, node, instruction = await NodeAgentWorkerService.build_context(db, run_id)
            log_event(
                "node_agent_worker_context_built",
                "completed",
                run_id=run_id,
                node_id=run.node_id,
            )
            if NodeAgentWorkerService._run_mode() == "containerized":
                await NodeAgentWorkerService._run_containerized(db, run, ctx, node)
            else:
                await NodeAgentWorkerService._run_provider_only(
                    db, run, ctx, node, instruction, run_id=run_id,
                )
            log_event("node_agent_worker_completed", "completed", run_id=run_id, node_id=run.node_id)
        except NodeContainerError as exc:
            await NodeAgentWorkerService._fail_run(db, run, "failed", exc.error_code)
            log_event(
                "node_agent_worker_container_failed",
                "failed",
                run_id=run_id,
                detail={"error_code": exc.error_code},
            )
        except _ProviderTimeout:
            await NodeAgentWorkerService._fail_run(db, run, "timed_out", "timeout")
            log_event("node_agent_worker_provider_failed", "failed", run_id=run_id, detail={"error_code": "timeout"})
        except DeepSeekProviderError as exc:
            status = NodeAgentWorkerService._status_for_deepseek_error(exc.error_code)
            await NodeAgentWorkerService._fail_run(db, run, status, exc.error_code)
            log_event(
                "node_agent_worker_provider_failed",
                "failed",
                run_id=run_id,
                detail={"error_code": exc.error_code, "provider": "deepseek"},
            )
        except _ProposalValidationError as exc:
            await NodeAgentWorkerService._fail_run(db, run, "failed", exc.error_code)
            log_event("node_agent_worker_failed", "failed", run_id=run_id, detail={"error_code": exc.error_code})
        except Exception as exc:
            await NodeAgentWorkerService._fail_run(db, run, "failed", type(exc).__name__)
            log_event("node_agent_worker_failed", "failed", run_id=run_id, detail={"error_code": type(exc).__name__})

    @staticmethod
    async def build_context(
        db: AsyncSession,
        run_id: str,
    ) -> tuple[AgentContext, NodeRecord, str]:
        result = await db.execute(select(NodeAgentRunRecord).where(NodeAgentRunRecord.id == run_id))
        run = result.scalar_one()
        node_result = await db.execute(select(NodeRecord).where(NodeRecord.id == run.node_id))
        node = node_result.scalar_one()
        instruction = node.goal or f"Implement node {node.plan_node_id}"

        boundary = NodeAgentWorkerService._node_boundary(node)
        allowed_files = boundary["write_set"]

        accessible_context = await NodeService.get_accessible_context(db, node.id)
        node_tests = node.tests if isinstance(node.tests, list) else []
        sandbox_policy = SandboxPolicy.for_run(
            run_id=run_id,
            node_id=node.id,
            workspace_root=get_config().workspace,
            allowed_files=allowed_files,
            node_tests=node_tests,
        )
        tool_capabilities = CapabilityPolicyService.for_run(
            allowed_files=allowed_files,
            node_tests=node_tests,
            sandbox_snapshot=sandbox_policy.snapshot(),
        )
        if NodeAgentWorkerService._run_mode() == "containerized":
            workspace = ContainerWorkspaceBuilder(get_config().workspace).build_node_workspace(
                run_id=run_id,
                node_id=node.id,
                read_set=boundary["read_set"],
                write_set=boundary["write_set"],
                readonly_context=boundary["readonly_context"],
                interfaces=node.interfaces if isinstance(node.interfaces, dict) else {},
                tests=node_tests,
                metrics=node.metrics if isinstance(node.metrics, (dict, list)) else {},
                conflict_contributions=boundary["conflict_contributions"],
            )
            tool_capabilities["container_workspace"] = {
                "root": str(workspace.root),
                "manifest_path": str(workspace.manifest_path),
                "output_dir": str(workspace.output_dir),
                "aggregate_dir": str(workspace.aggregate_dir),
            }
        ctx = AgentContext(
            instruction=instruction,
            node={
                "id": node.plan_node_id,
                "title": node.title,
                "goal": node.goal,
                "node_type": node.node_type,
                "depends_on": node.depends_on,
            },
            allowed_files=allowed_files,
            tests=node_tests,
            metrics=node.metrics if isinstance(node.metrics, dict) else {},
            constraints=node.constraints if isinstance(node.constraints, dict) else {},
            review_checks=node.review_checks if isinstance(node.review_checks, list) else [],
            expected_outputs=node.expected_outputs if isinstance(node.expected_outputs, dict) else {},
            accessible_context=accessible_context,
            tool_capabilities=tool_capabilities,
        )
        return ctx, node, instruction

    @staticmethod
    async def submit_provider_result(
        db: AsyncSession,
        run_id: str,
        node: NodeRecord,
        instruction: str,
        proposal: AgentProposalSchema,
        accessible_context: dict,
    ) -> str:
        allowed_files = NodeAgentWorkerService._node_boundary(node)["write_set"]
        record = ProposalRecord(
            node_id=node.id,
            plan_node_id=node.plan_node_id,
            instruction=instruction,
            allowed_files=allowed_files,
            accessible_context=accessible_context,
            proposal=proposal.model_dump(),
            status="proposed",
            source="node_agent_worker",
        )
        db.add(record)
        await db.flush()
        return record.id

    @staticmethod
    async def _call_provider(ctx: AgentContext, run: NodeAgentRunRecord) -> AgentProposalSchema:
        provider = AgentProviderFactory.create(context=ctx)
        cfg = AgentProviderFactory.get_config()
        timeout = float(cfg["timeout_seconds"])
        log_event(
            "node_agent_worker_provider_called",
            "started",
            run_id=run.id,
            node_id=run.node_id,
            detail={"provider": provider.name},
        )
        start = time.monotonic()
        try:
            proposal = await asyncio.wait_for(provider.generate(ctx), timeout=timeout)
        except asyncio.TimeoutError:
            raise _ProviderTimeout()
        except DeepSeekProviderError as exc:
            if exc.error_code == "deepseek_timeout":
                raise _ProviderTimeout() from exc
            raise
        if not proposal.summary or not proposal.summary.strip():
            raise _ProposalValidationError("EmptySummary")
        file_patches = [fp.model_dump() for fp in proposal.file_patches]
        errors = ProposalPathValidator.validate(file_patches, ctx.allowed_files)
        if errors:
            raise _ProposalValidationError("PathBoundaryError")
        cmd_errors = NodeAgentWorkerService._validate_tests_to_run(ctx, proposal)
        if cmd_errors:
            raise _ProposalValidationError("CommandPolicyError")
        _ = int((time.monotonic() - start) * 1000)
        return proposal

    @staticmethod
    def _validate_tests_to_run(ctx: AgentContext, proposal: AgentProposalSchema) -> list[str]:
        snap = ctx.tool_capabilities.get("sandbox", {}) if ctx.tool_capabilities else {}
        return validate_proposal_tests_to_run(proposal, snap, ctx.tests)

    @staticmethod
    def _status_for_deepseek_error(error_code: str) -> str:
        if error_code == "deepseek_timeout":
            return "timed_out"
        if error_code in ("deepseek_rate_limited", "deepseek_server_error"):
            return "failed_retryable"
        return "failed"

    @staticmethod
    async def _run_sandbox_tests(
        run_id: str,
        node_id: str,
        node: NodeRecord,
        proposal: AgentProposalSchema,
    ) -> dict | None:
        commands = [str(c).strip() for c in proposal.tests_to_run if str(c).strip()]
        if not commands:
            return None

        allowed_files = NodeAgentWorkerService._node_boundary(node)["write_set"]
        node_tests = node.tests if isinstance(node.tests, list) else []
        policy = SandboxPolicy.for_run(
            run_id=run_id,
            node_id=node_id,
            workspace_root=get_config().workspace,
            allowed_files=[f for f in allowed_files if f],
            node_tests=node_tests,
        )
        executor = SandboxedToolExecutor(policy)
        result = await executor.run_allowed_tests(commands)
        log_event(
            "node_agent_worker_sandbox_tests",
            result.get("status", "completed"),
            run_id=run_id,
            node_id=node_id,
            detail={
                "command_count": len(commands),
                "error_code": result.get("error_code"),
            },
        )
        return result

    @staticmethod
    def _node_boundary(node: NodeRecord) -> dict:
        _clean_constraints, boundary = unpack_container_boundary(node.constraints)
        write_set_raw = boundary.get("write_set") or list(node.files or [])
        read_set_raw = boundary.get("read_set") or []
        readonly_context_raw = boundary.get("readonly_context") or []
        return {
            "write_set": NodeAgentWorkerService._normalize_paths(write_set_raw),
            "read_set": NodeAgentWorkerService._normalize_paths(read_set_raw),
            "readonly_context": NodeAgentWorkerService._normalize_paths(readonly_context_raw),
            "conflict_contributions": list(boundary.get("conflict_contributions") or []),
            "container_policy": dict(boundary.get("container_policy") or {}),
        }

    @staticmethod
    def _normalize_paths(paths: list) -> list[str]:
        seen: set[str] = set()
        normalized: list[str] = []
        for value in paths:
            key = ProposalPathValidator.normalize_workspace_relative(str(value))
            if not key or key in seen:
                continue
            seen.add(key)
            normalized.append(key)
        return normalized

    @staticmethod
    async def _run_provider_only(
        db: AsyncSession,
        run: NodeAgentRunRecord,
        ctx: AgentContext,
        node: NodeRecord,
        instruction: str,
        *,
        run_id: str,
    ) -> None:
        proposal = await NodeAgentWorkerService._call_provider(ctx, run)
        test_results = await NodeAgentWorkerService._run_sandbox_tests(
            run_id, run.node_id, node, proposal,
        )
        proposal_id = await NodeAgentWorkerService.submit_provider_result(
            db, run_id, node, instruction, proposal, ctx.accessible_context,
        )
        await NodeAgentWorkerService._complete_run(
            db, run, proposal_id, proposal.summary, test_results=test_results,
        )

    @staticmethod
    async def _run_containerized(
        db: AsyncSession,
        run: NodeAgentRunRecord,
        ctx: AgentContext,
        node: NodeRecord,
    ) -> None:
        workspace = get_config().workspace
        orchestrator = NodeContainerOrchestrator(workspace)
        container_id: str | None = None
        try:
            await NodeAgentWorkerService._start_node_container(db, run, ctx, orchestrator=orchestrator)
            container_id = run.container_id
            boundary = NodeAgentWorkerService._node_boundary(node)
            main_meta = MainAgentContainerService(workspace).read_for_session(run.session_id)
            baseline = (main_meta or {}).get("git_baseline_revision", "")
            if NodeAgentWorkerService._container_output_missing(run.id):
                if ContainerOutputSimulator.should_simulate(workspace):
                    ContainerOutputSimulator(workspace).write_for_run(
                        run_id=run.id,
                        node_id=node.plan_node_id,
                        baseline_revision=baseline,
                        write_files=boundary["write_set"],
                        aggregate_contributions=[
                            {
                                "path": c.get("contribution_path"),
                                "aggregate_target": c.get("aggregate_target"),
                            }
                            for c in boundary.get("conflict_contributions", [])
                            if c.get("contribution_path")
                        ],
                    )
                if NodeAgentWorkerService._container_output_missing(run.id):
                    raise NodeContainerError("container_output_missing")
            integration = await NodeAgentWorkerService._integrate_container_outputs(db, run, node)
            manifest = NodeAgentWorkerService._read_manifest(run.id)
            await NodeAgentWorkerService._complete_containerized_run(db, run, manifest, integration)
        except NodeContainerError:
            raise
        except Exception as exc:
            message = str(exc)
            if "git_baseline_mismatch" in message or "manifest_baseline_mismatch" in message:
                raise NodeContainerError("integration_rejected_by_baseline", message=message) from exc
            if "validation_command_failed" in message:
                raise NodeContainerError("aggregate_validation_failed", message=message) from exc
            code = "integration_failed" if isinstance(exc, ValueError) else type(exc).__name__
            raise NodeContainerError(code, message=message) from exc
        finally:
            if container_id:
                orchestrator.cleanup_container(container_id)

    @staticmethod
    def _read_manifest(run_id: str) -> dict:
        import json

        path = (
            get_config().workspace
            / ".aicoding"
            / "container-workspaces"
            / run_id
            / "output"
            / "manifest.json"
        )
        return json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    async def _start_node_container(
        db: AsyncSession,
        run: NodeAgentRunRecord,
        ctx: AgentContext,
        *,
        orchestrator: NodeContainerOrchestrator | None = None,
    ) -> None:
        container_workspace = ctx.tool_capabilities.get("container_workspace") if ctx.tool_capabilities else None
        if not container_workspace:
            raise NodeContainerError("container_workspace_missing")
        workspace_root = Path(container_workspace["root"])
        orch = orchestrator or NodeContainerOrchestrator(get_config().workspace)
        meta = orch.run_node_container(
            run_id=run.id,
            node_id=run.node_id,
            workspace_root=workspace_root,
        )
        run.container_id = meta.get("container_id")
        run.container_status = meta.get("container_status")
        run.container_health = meta.get("container_health")
        run.container_error = meta.get("container_error")
        run.container_logs_summary = meta.get("logs_summary")
        run.diagnostic_path = meta.get("diagnostic_path")
        await db.commit()

    @staticmethod
    def _container_output_missing(run_id: str) -> bool:
        manifest = (
            get_config().workspace
            / ".aicoding"
            / "container-workspaces"
            / run_id
            / "output"
            / "manifest.json"
        )
        return not manifest.exists()

    @staticmethod
    async def _integrate_container_outputs(
        db: AsyncSession,
        run: NodeAgentRunRecord,
        node: NodeRecord,
    ) -> dict:
        boundary = NodeAgentWorkerService._node_boundary(node)
        main_meta = MainAgentContainerService(get_config().workspace).read_for_session(run.session_id)
        if not main_meta or not main_meta.get("git_baseline_revision"):
            raise ValueError("missing_session_baseline")
        aggregate_paths = [
            str(c.get("contribution_path", ""))
            for c in boundary.get("conflict_contributions", [])
            if c.get("contribution_path")
        ]
        strategies = load_aggregate_strategies(get_config().workspace)
        return NodeOutputIntegrationService(get_config().workspace).integrate_run(
            run_id=run.id,
            session_id=run.session_id,
            allowed_files=boundary["write_set"],
            allowed_aggregate_paths=aggregate_paths,
            expected_baseline_revision=main_meta["git_baseline_revision"],
            aggregate_strategies=strategies,
        )

    @staticmethod
    async def _complete_containerized_run(
        db: AsyncSession,
        run: NodeAgentRunRecord,
        manifest: dict,
        integration: dict,
    ) -> None:
        now = utc_now_naive()
        summary = str(manifest.get("summary", ""))[:500]
        test_results = manifest.get("test_results", {})
        metrics = manifest.get("metrics", {})
        payload = {
            "integration": integration,
            "test_results": test_results,
            "metrics": metrics,
            "test_summary": NodeAgentWorkerService._summarize_tests(test_results),
            "metrics_summary": NodeAgentWorkerService._summarize_metrics(metrics),
        }
        result_rec = NodeAgentResultRecord(
            run_id=run.id,
            node_id=run.node_id,
            status="completed",
            result_type="container_integration",
            proposal_id=None,
            summary=summary,
            recommended_next_action="human_review",
            payload=payload,
        )
        db.add(result_rec)
        run.status = "completed"
        run.phase = "finalizing"
        run.finished_at = now
        if run.started_at:
            run.duration_ms = int((now - run.started_at).total_seconds() * 1000)
        run.result_summary = summary
        checkpoint = integration.get("checkpoint", {})
        if checkpoint.get("baseline_revision"):
            NodeAgentWorkerService._update_session_baseline(run.session_id, checkpoint["baseline_revision"])
        await NodeAgentRunService.release_lock(db, run.node_id)
        await db.commit()

    @staticmethod
    def _summarize_tests(test_results: dict) -> str:
        tests = test_results.get("tests") if isinstance(test_results, dict) else []
        if not isinstance(tests, list) or not tests:
            return ""
        passed = sum(1 for t in tests if isinstance(t, dict) and t.get("status") == "passed")
        return f"{passed}/{len(tests)} passed"

    @staticmethod
    def _summarize_metrics(metrics: dict) -> str:
        items = metrics.get("items") if isinstance(metrics, dict) else []
        if not isinstance(items, list) or not items:
            return ""
        ok = sum(1 for m in items if isinstance(m, dict) and m.get("status") == "ok")
        return f"{ok}/{len(items)} ok"

    @staticmethod
    def _update_session_baseline(session_id: str, baseline_revision: str) -> None:
        path = get_config().workspace / ".aicoding" / "main-agent-containers" / f"{session_id}.json"
        if not path.exists():
            return
        import json

        metadata = json.loads(path.read_text(encoding="utf-8"))
        metadata["git_baseline_revision"] = baseline_revision
        metadata["baseline_revision"] = baseline_revision
        path.write_text(json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8")

    @staticmethod
    def _run_mode() -> str:
        value = os.getenv("BRIDLE_NODE_AGENT_RUN_MODE", "provider_only").strip().lower()
        if value not in {"provider_only", "containerized"}:
            return "provider_only"
        return value

    @staticmethod
    async def _complete_run(
        db: AsyncSession,
        run: NodeAgentRunRecord,
        proposal_id: str,
        summary: str,
        *,
        test_results: dict | None = None,
    ) -> None:
        now = utc_now_naive()
        payload: dict = {"proposal_id": proposal_id}
        if test_results is not None:
            payload["sandbox_test_results"] = test_results
        result_rec = NodeAgentResultRecord(
            run_id=run.id,
            node_id=run.node_id,
            status="completed",
            result_type="proposal",
            proposal_id=proposal_id,
            summary=summary[:500],
            recommended_next_action="human_review",
            payload=payload,
        )
        db.add(result_rec)
        run.status = "completed"
        run.phase = "finalizing"
        run.finished_at = now
        if run.started_at:
            run.duration_ms = int((now - run.started_at).total_seconds() * 1000)
        run.result_summary = summary[:500]
        await NodeAgentRunService.release_lock(db, run.node_id)
        await db.commit()

    @staticmethod
    async def _fail_run(db: AsyncSession, run: NodeAgentRunRecord, status: str, error_code: str) -> None:
        now = utc_now_naive()
        run.status = status
        run.blocked_reason = error_code
        run.finished_at = now
        await NodeAgentRunService.release_lock(db, run.node_id)
        await db.commit()


class _ProviderTimeout(Exception):
    pass


class _ProposalValidationError(Exception):
    def __init__(self, error_code: str) -> None:
        self.error_code = error_code
        super().__init__(error_code)
