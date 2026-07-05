"""Unified project-session Agent Gateway."""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from bridle.agent.container.candidate_contract import (
    CandidateExecutionResult,
    compute_patches,
    persist_result,
    snapshot_directory_hashes,
)
from bridle.agent.container.candidate_service import CandidateExecutionService
from bridle.agent.container.container_service import get_shared_container_backend
from bridle.agent.container.test_backend import ModuleContainerTestBackend
from bridle.agent.container.test_command_compiler import TestCommandCompiler
from bridle.agent.memory.short_term_memory import ShortTermMemory
from bridle.agent.providers.agent_provider import AgentProviderFactory
from bridle.agent.runtime.role_policy import RuntimeRolePolicy
from bridle.agent.runtime.schemas import AgentContext
from bridle.agent.skills.registry import SkillRegistry
from bridle.api.errors import ConflictError
from bridle.features.project_map.patch_schemas import PlanPatchSchema
from bridle.features.project_map.plan_service import PlanService
from bridle.features.project_map.service import ProjectMapService
from bridle.features.sessions.schemas import ProjectMessageCreateSchema, ProjectMessageReadSchema
from bridle.features.sessions.service import ProjectSessionService
from bridle.logging.facade import get_logging_facade


class AgentGateway:
    """Run planning and execution turns through one project-scoped runtime."""

    @staticmethod
    async def converse(
        db: AsyncSession,
        session_id: str,
        content: str,
        *,
        node_id: str | None = None,
    ) -> ProjectMessageReadSchema:
        """Run one shared turn; session/content/node input exits as a persisted assistant message."""
        session = await ProjectSessionService.get(db, session_id)
        if not session.available or not Path(session.project_path).is_dir():
            raise ConflictError(
                resource="project_session",
                message="Project path is unavailable; history is read-only",
                error_code="project_unavailable_read_only",
            )
        facade = get_logging_facade()
        started = time.monotonic()
        facade.info_event(
            "project_agent_turn",
            "started",
            session_id=session_id,
            detail={"project_id": session.project_id, "role": session.role},
        )
        execution_node: dict | None = None
        candidate_setup = None
        container_test_backend = None
        try:
            store = await ProjectMapService.store_for(db, session.project_id)
            readiness = store.readiness()
            if not readiness["can_chat"]:
                raise ConflictError(
                    resource="project_map",
                    message="Project map is not ready for chat",
                    error_code="project_map_not_ready",
                    details=readiness,
                )
            if session.role == "executing":
                if node_id is None:
                    raise ConflictError(
                        resource="project_session",
                        message="Executing turns require an explicit plan node",
                        error_code="execution_node_required",
                    )
                execution_node = store.get_node(node_id)
                if execution_node["status"] != "running":
                    execution_node = store.start_node(node_id)
            elif node_id is not None:
                raise ConflictError(
                    resource="project_session",
                    message="Planning turns cannot select an execution node",
                    error_code="planning_node_forbidden",
                )

            await ProjectSessionService.create_message(
                db,
                session_id,
                ProjectMessageCreateSchema(role="user", content=content),
            )
            messages = await ProjectSessionService.list_messages(db, session_id)
            memory_input = [message.model_dump(mode="json") for message in messages]
            memory = ShortTermMemory(run_id=session_id).compact(memory_input)
            overview = store.overview()
            skill_ids = SkillRegistry.default().list_ids()
            capabilities = RuntimeRolePolicy.manifest(session.role)
            allowed_files = [] if execution_node is None else list(execution_node.get("files") or [])
            node_tests = [] if execution_node is None else list(execution_node.get("tests") or [])
            readonly_files: list[str] = []
            workspace_root = session.project_path
            candidate_id: str | None = None
            if execution_node is not None:
                readonly_files = store.mock_readonly_paths_for_node(execution_node["id"])
                allowed_files = sorted(set(allowed_files) | set(readonly_files))
                snapshot = store.module_execution_snapshot(execution_node["id"])
                if snapshot.get("error_code"):
                    raise ConflictError(
                        resource="module_boundary",
                        message="Module execution snapshot is incomplete",
                        error_code=str(snapshot["error_code"]),
                        details=snapshot.get("detail") or {},
                    )
                candidate_service = CandidateExecutionService(session.project_path)
                candidate_setup = candidate_service.prepare(
                    run_id=session_id,
                    node=execution_node,
                    base_map_seq=store.latest_change_seq(),
                    readonly_files=readonly_files,
                    map_snapshot=snapshot,
                )
                candidate_id = candidate_setup.candidate_id
                workspace_root = str(candidate_setup.workspace.project_dir)
                allowed_files = sorted(set(allowed_files) | set(candidate_setup.workspace.write_set))
                node_tests = list(snapshot.get("test_commands") or node_tests)
                facade.info_event(
                    "candidate_created",
                    "completed",
                    session_id=session_id,
                    detail={
                        "project_id": session.project_id,
                        "node_id": execution_node["id"],
                        "candidate_id": candidate_id,
                        "module_id": candidate_setup.module_id,
                    },
                )
                approved_commands = TestCommandCompiler.compile_commands(
                    test_commands=node_tests,
                    test_entity_id=execution_node["id"],
                    map_seq=store.latest_change_seq(),
                )
                container_backend = get_shared_container_backend(session.project_path)
                container_test_backend = ModuleContainerTestBackend(
                    container_backend,
                    candidate_request=candidate_setup.request,
                    candidate_root=str(candidate_setup.workspace.root),
                    module_root=str(candidate_setup.workspace.module_root),
                    candidate_rel=candidate_setup.workspace.candidate_rel,
                    test_entity_id=execution_node["id"],
                    required_commands=node_tests,
                    required_command_ids=[cmd.command_id for cmd in approved_commands],
                    map_seq=store.latest_change_seq(),
                )
            context_node = execution_node or {
                "id": "project-runtime",
                "title": session.title,
                "goal": "Continue the project plan and execute only when permitted.",
                "node_type": "project_session",
                "depends_on": [],
            }
            capabilities["sandbox"] = {
                "run_id": session_id,
                "node_id": context_node["id"],
                "workspace_root": workspace_root,
                "allowed_files": allowed_files,
                "readonly_files": readonly_files,
                "node_tests": node_tests,
                "network_allowed": False,
                "candidate_id": candidate_id,
            }
            context = AgentContext(
                instruction=content,
                node=context_node,
                allowed_files=allowed_files,
                tests=node_tests,
                accessible_context={
                    "memory": memory,
                    "project_map": overview,
                    "skill_ids": skill_ids,
                    "session_role": session.role,
                },
                tool_capabilities=capabilities,
            )

            async def read_project_map(arguments: dict) -> dict:
                """Read one bounded map view; tool arguments exit through the existing store queries."""
                RuntimeRolePolicy.require(session.role, "read_project_map")
                mode = str(arguments.get("mode", "overview"))
                limit = max(1, min(int(arguments.get("limit", 50)), 200))
                if mode == "overview":
                    return store.overview()
                if mode == "node":
                    return store.get_node(str(arguments.get("node_id", "")))
                if mode == "children":
                    return store.children(
                        parent_id=arguments.get("parent_id"),
                        cursor=arguments.get("cursor"),
                        limit=limit,
                    )
                if mode == "subgraph":
                    depth = max(0, min(int(arguments.get("depth", 1)), 5))
                    return store.subgraph(str(arguments.get("node_id", "")), depth=depth, limit=limit)
                if mode == "search":
                    return store.search(
                        str(arguments.get("query", "")),
                        cursor=arguments.get("cursor"),
                        limit=limit,
                    )
                raise ValueError("Unsupported project map read mode")

            async def read_code_map(arguments: dict) -> dict:
                """Progressive code-map queries with budget enforcement."""
                RuntimeRolePolicy.require(session.role, "read_code_map")
                mode = str(arguments.get("mode", "neighbors"))
                max_nodes = max(1, min(int(arguments.get("max_nodes", 50)), 200))
                entity_id = str(arguments.get("entity_id", "")).strip()
                seed_id = arguments.get("seed_id")
                mapping_seed = None
                if session.role == "mapping":
                    if not seed_id:
                        raise ConflictError(
                            resource="map_blind_spot",
                            message="Mapping queries require an open blind spot seed",
                            error_code="blind_spot_seed_required",
                        )
                    mapping_seed = str(seed_id)
                if mode == "node":
                    return store.map_get_node(entity_id, mapping_seed=mapping_seed)
                if mode == "neighbors":
                    return store.map_neighbors(
                        entity_id,
                        kinds=arguments.get("kinds"),
                        max_nodes=max_nodes,
                        mapping_seed=mapping_seed,
                    )
                if mode == "subgraph":
                    depth = max(0, min(int(arguments.get("depth", 1)), 5))
                    return store.map_subgraph(
                        entity_id,
                        depth=depth,
                        max_nodes=max_nodes,
                        kinds=arguments.get("kinds"),
                        mapping_seed=mapping_seed,
                    )
                if mode == "read_span":
                    max_tokens = max(500, min(int(arguments.get("max_tokens", 8000)), 32000))
                    return store.map_read_span(entity_id, max_tokens=max_tokens, mapping_seed=mapping_seed)
                if mode == "blind_spots":
                    return store.map_blind_spots(
                        seed_id=str(seed_id) if seed_id else None,
                        max_nodes=max_nodes,
                        require_seed=session.role == "mapping",
                    )
                raise ValueError("Unsupported code map read mode")

            async def propose_semantic_annotation(arguments: dict) -> dict:
                RuntimeRolePolicy.require(session.role, "propose_semantic_annotation")
                mapping_seed = None
                if session.role == "mapping":
                    seed_id = arguments.get("seed_id")
                    if not seed_id:
                        raise ConflictError(
                            resource="map_blind_spot",
                            message="Mapping queries require an open blind spot seed",
                            error_code="blind_spot_seed_required",
                        )
                    mapping_seed = str(seed_id)
                return store.propose_semantic_annotation(
                    source_id=str(arguments.get("source_id", "")),
                    summary=str(arguments.get("summary", "")),
                    evidence=dict(arguments.get("evidence") or {}),
                    model=str(arguments.get("model", "agent")),
                    confidence=float(arguments.get("confidence", 0.0)),
                    file_hash=str(arguments.get("file_hash", "")),
                    risk=str(arguments.get("risk", "low")),
                    mapping_seed=mapping_seed,
                )

            async def dispatch_child_agent(arguments: dict) -> dict:
                RuntimeRolePolicy.require(session.role, "dispatch_child_agent")
                return store.dispatch_child_agent(
                    str(arguments.get("node_id", "")),
                    target_role=str(arguments.get("target_role", "mapping")),
                )

            async def patch_plan_nodes(arguments: dict) -> dict:
                """Apply a local plan patch; tool arguments exit only through PlanService.patch_current."""
                RuntimeRolePolicy.require(session.role, "patch_plan_nodes")
                patch = PlanPatchSchema.model_validate(arguments)
                return await PlanService.patch_current(db, session.project_id, patch)

            async def select_node(arguments: dict) -> dict:
                """Confirm this turn's node; tool input exits without changing its fixed sandbox."""
                RuntimeRolePolicy.require(session.role, "select_node")
                requested_id = str(arguments.get("node_id", "")).strip()
                if not requested_id:
                    raise ValueError("node_id is required")
                if execution_node is None or requested_id != execution_node["id"]:
                    raise ConflictError(
                        resource="plan_node",
                        message="Cannot switch execution nodes during an active turn",
                        error_code="execution_node_switch_forbidden",
                    )
                return execution_node

            provider = AgentProviderFactory.create(
                context,
                runtime_tool_handlers={
                    "read_project_map": read_project_map,
                    "read_code_map": read_code_map,
                    "propose_semantic_annotation": propose_semantic_annotation,
                    "dispatch_child_agent": dispatch_child_agent,
                    "patch_plan_nodes": patch_plan_nodes,
                    "select_node": select_node,
                },
                test_backend=container_test_backend,
            )
            provider_config = AgentProviderFactory.get_config()
            proposal = await asyncio.wait_for(
                provider.generate(context),
                timeout=float(provider_config["timeout_seconds"]),
            )
            assistant = await ProjectSessionService.create_message(
                db,
                session_id,
                ProjectMessageCreateSchema(role="assistant", content=proposal.summary),
            )
            if candidate_setup is not None:
                AgentGateway._persist_candidate_outcome(
                    candidate_setup=candidate_setup,
                    execution_node=execution_node,
                    container_test_backend=container_test_backend,
                    session_id=session_id,
                    project_id=session.project_id,
                    facade=facade,
                )
            facade.info_event(
                "project_agent_turn",
                "completed",
                session_id=session_id,
                duration_ms=int((time.monotonic() - started) * 1000),
                detail={
                    "project_id": session.project_id,
                    "role": session.role,
                    "memory_count": len(memory),
                    "root_count": len(overview["roots"]),
                    "skill_count": len(skill_ids),
                    "provider": provider.name,
                },
            )
            return assistant
        except Exception as exc:
            if candidate_setup is not None:
                AgentGateway._persist_candidate_outcome(
                    candidate_setup=candidate_setup,
                    execution_node=execution_node,
                    container_test_backend=container_test_backend,
                    session_id=session_id,
                    project_id=session.project_id,
                    facade=facade,
                    fallback_error_code=type(exc).__name__,
                )
            facade.info_event(
                "project_agent_turn",
                "failed",
                session_id=session_id,
                duration_ms=int((time.monotonic() - started) * 1000),
                error_code=type(exc).__name__,
                detail={"project_id": session.project_id, "role": session.role},
            )
            raise

    @staticmethod
    def _persist_candidate_outcome(
        *,
        candidate_setup,
        execution_node: dict | None,
        container_test_backend: ModuleContainerTestBackend | None,
        session_id: str,
        project_id: str,
        facade,
        fallback_error_code: str | None = None,
    ) -> None:
        base_hashes = snapshot_directory_hashes(
            candidate_setup.workspace.baseline_dir,
            list(candidate_setup.request.write_set),
        )
        candidate_hashes = snapshot_directory_hashes(
            candidate_setup.workspace.project_dir,
            list(candidate_setup.request.write_set),
        )
        changed, patches = compute_patches(
            base_hashes=base_hashes,
            candidate_hashes=candidate_hashes,
            write_set=list(candidate_setup.request.write_set),
        )
        evidence = container_test_backend.collect_evidence() if container_test_backend else None
        if fallback_error_code:
            status = "blocked"
            error_code = fallback_error_code
            event = "candidate_blocked"
        elif evidence and evidence.all_required_passed and evidence.required_command_ids:
            status = "ready"
            error_code = None
            event = "candidate_ready"
        else:
            status = "blocked"
            error_code = (evidence.error_code if evidence else None) or "verification_incomplete"
            event = "candidate_blocked"
        container_info: dict = {}
        if evidence and evidence.container_runs:
            container_info = dict(evidence.container_runs[-1])
        result = CandidateExecutionResult(
            status=status,
            changed_paths=tuple(changed),
            patches=tuple(patches),
            base_hashes=base_hashes,
            candidate_hashes=candidate_hashes,
            test_results=tuple(evidence.test_runs if evidence else ()),
            container=container_info,
            diagnostic_path=str(candidate_setup.workspace.diagnostics_dir),
            error_code=error_code,
            candidate_id=candidate_setup.candidate_id,
            base_map_seq=candidate_setup.request.base_map_seq,
            verification=evidence.to_dict() if evidence else None,
        )
        persist_result(result, candidate_setup.workspace.root)
        facade.info_event(
            event,
            "completed" if status == "ready" else "failed",
            session_id=session_id,
            detail={
                "project_id": project_id,
                "node_id": execution_node["id"] if execution_node else None,
                "candidate_id": candidate_setup.candidate_id,
                "changed_count": len(changed),
                "error_code": error_code,
            },
        )

