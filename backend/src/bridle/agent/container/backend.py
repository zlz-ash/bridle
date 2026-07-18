"""Agent container execution backend for module-scoped candidate runs."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from bridle.agent.container.active_slot import (
    ActiveSlotLayout,
    build_slot_mounts,
    slot_allowed_mount_roots,
    tree_hashes,
)
from bridle.agent.container.container_control import (
    EXECUTION_EXITED,
    EXECUTION_PHASE_FINALIZE,
    EXECUTION_STARTED_UNKNOWN,
    ControlEnvelopeError,
    HostAttestationContext,
    accept_control_evidence,
    begin_run_evidence,
    parse_control_envelope_from_exec_output,
    persist_control_evidence,
    persist_failed_run_evidence,
)
from bridle.agent.container.container_identity import build_container_labels
from bridle.agent.container.env import build_agent_container_env
from bridle.agent.container.image_identity import resolve_image_identity
from bridle.agent.container.lifecycle import build_module_container_name
from bridle.agent.container.orchestrator import ContainerOrchestrator, OrchestrationError
from bridle.agent.container.runner import ContainerRequest, ContainerRunner
from bridle.agent.container.runner_factory import resolve_container_runner
from bridle.agent.container.test_command_compiler import TestCommandCompiler

logger = logging.getLogger("bridle")

_KEEP_ALIVE_COMMAND = ["python", "-m", "bridle.agent.container.entrypoint", "--keep-alive"]
_RUN_TASK_COMMAND = ["python", "-m", "bridle.agent.container.entrypoint", "--run-task"]


class AgentContainerError(Exception):
    def __init__(self, error_code: str, *, message: str = "", detail: dict[str, Any] | None = None) -> None:
        self.error_code = error_code
        self.detail = detail or {}
        super().__init__(message or error_code)


class AgentContainerBackend:
    """Run tests and commands inside a reusable module container."""

    def __init__(self, workspace_root: str | Path, *, runner: ContainerRunner | None = None) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        resolved_runner = resolve_container_runner(self.workspace_root, runner=runner)
        self._orchestrator = ContainerOrchestrator(resolved_runner, self.workspace_root)
        self._runner = resolved_runner

    @property
    def orchestrator(self) -> ContainerOrchestrator:
        return self._orchestrator

    def run_command_in_candidate(
        self,
        *,
        candidate_root: Path,
        module_root: Path,
        candidate_rel: str,
        run_id: str,
        node_id: str,
        module_id: str,
        boundary_fingerprint: str,
        command: str,
        write_set: list[str],
        map_seq: int,
        timeout_seconds: int = 300,
        network_allowed: bool = False,
        image: str = "bridle-agent:local",
        image_version: str = "local",
    ) -> dict[str, Any]:
        logger.info(
            "container_command_started",
            extra={
                "action": "container_command_started",
                "status": "started",
                "detail": {"run_id": run_id, "node_id": node_id, "module_id": module_id},
            },
        )
        request_manifest = {
            "schema": "bridle.container_test_request/v1",
            "commands": [
                {
                    "command_id": "exploratory-command",
                    "argv": ["bash", "-lc", command],
                    "raw_command": command,
                    "test_entity_id": "exploratory-command",
                    "map_seq": map_seq,
                }
            ],
            "write_set": write_set,
            "map_seq": map_seq,
            "test_entity_id": "exploratory-command",
            "red_verification": False,
            "protected_hashes": {
                "baseline": tree_hashes(candidate_root.resolve() / "baseline"),
                "mocks": tree_hashes(candidate_root.resolve() / "mocks"),
            },
        }
        result = self.run_tests_in_candidate(
            candidate_root=candidate_root,
            module_root=module_root,
            candidate_rel=candidate_rel,
            run_id=run_id,
            node_id=node_id,
            module_id=module_id,
            boundary_fingerprint=boundary_fingerprint,
            test_commands=[],
            write_set=write_set,
            test_entity_id="exploratory-command",
            map_seq=map_seq,
            timeout_seconds=timeout_seconds,
            network_allowed=network_allowed,
            image=image,
            image_version=image_version,
            _request_manifest=request_manifest,
        )
        logger.info(
            "container_command_completed",
            extra={
                "action": "container_command_completed",
                "status": "completed",
                "detail": {"run_id": run_id, "node_id": node_id, "module_id": module_id},
            },
        )
        return result

    def run_tests_in_candidate(
        self,
        *,
        candidate_root: Path,
        module_root: Path,
        candidate_rel: str,
        run_id: str,
        node_id: str,
        module_id: str,
        boundary_fingerprint: str,
        test_commands: list[str],
        write_set: list[str],
        test_entity_id: str,
        map_seq: int,
        timeout_seconds: int = 300,
        network_allowed: bool = False,
        image: str = "bridle-agent:local",
        image_version: str = "local",
        replace_container: bool = False,
        red_verification: bool = False,
        _request_manifest: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        candidate_root = candidate_root.resolve()
        module_root = module_root.resolve()
        diag_dir = candidate_root / "diagnostics"
        if _request_manifest is None:
            approved = TestCommandCompiler.compile_commands(
                test_commands=test_commands,
                test_entity_id=test_entity_id,
                map_seq=map_seq,
            )
            request_manifest = {
                "schema": "bridle.container_test_request/v1",
                "commands": TestCommandCompiler.manifest_commands(approved),
                "write_set": write_set,
                "map_seq": map_seq,
                "test_entity_id": test_entity_id,
                "red_verification": red_verification,
                "protected_hashes": {
                    "baseline": tree_hashes(candidate_root / "baseline"),
                    "mocks": tree_hashes(candidate_root / "mocks"),
                },
            }
        else:
            request_manifest = _request_manifest
        diag_dir.mkdir(parents=True, exist_ok=True)
        (diag_dir / "test-request.json").write_text(json.dumps(request_manifest, indent=2), encoding="utf-8")
        resolved_image_id = resolve_image_identity(image)
        begin_run_evidence(
            candidate_root,
            run_id=run_id,
            candidate_rel=candidate_rel,
            node_id=node_id,
            test_entity_id=test_entity_id,
            image_digest=resolved_image_id,
        )

        def build_request(layout: ActiveSlotLayout) -> ContainerRequest:
            slot_mounts = build_slot_mounts(layout)
            labels = build_container_labels(
                project_root=self.workspace_root,
                module_id=module_id,
                boundary_fingerprint=boundary_fingerprint,
                image_version=resolved_image_id,
                mounts=slot_mounts,
            )
            env = build_agent_container_env(run_id=run_id, node_id=node_id, network_allowed=network_allowed)
            network_mode = "bridge" if network_allowed else "none"
            extra_hosts = ["host.docker.internal:host-gateway"] if network_allowed else None
            return ContainerRequest(
                name=build_module_container_name(
                    project_root=self.workspace_root,
                    module_id=module_id,
                    boundary_fingerprint=boundary_fingerprint,
                    image_version=resolved_image_id,
                ),
                image=image,
                image_id=resolved_image_id,
                run_user="1000",
                network_mode=network_mode,
                mounts=slot_mounts,
                environment=env,
                command=_KEEP_ALIVE_COMMAND,
                role="agent",
                timeout_seconds=timeout_seconds,
                allowed_mount_roots=slot_allowed_mount_roots(layout),
                extra_hosts=extra_hosts,
                module_id=module_id,
                boundary_fingerprint=boundary_fingerprint,
                image_version=resolved_image_id,
                keep_alive=True,
                read_only_root=True,
                module_mount_root=str(layout.slot_root),
                labels=labels,
            )

        logger.info(
            "container_test_started",
            extra={
                "action": "container_test_started",
                "status": "started",
                "detail": {
                    "run_id": run_id,
                    "node_id": node_id,
                    "candidate_root": str(candidate_root),
                    "module_root": str(module_root),
                    "candidate_rel": candidate_rel,
                    "module_id": module_id,
                },
            },
        )

        def exec_env(layout: ActiveSlotLayout) -> dict[str, str]:
            lease_path = layout.diagnostics / ".lease.json"
            token = ""
            if lease_path.is_file():
                token = str(json.loads(lease_path.read_text(encoding="utf-8")).get("token") or "")
            return {
                "BRIDLE_ACTIVE_SLOT": "1",
                "BRIDLE_LEASE_TOKEN": token,
                "BRIDLE_RUN_ID": run_id,
            }

        try:
            result = self._orchestrator.run_candidate_test_transaction(
                module_id=module_id,
                module_root=module_root,
                candidate_root=candidate_root,
                candidate_rel=candidate_rel,
                run_id=run_id,
                boundary_fingerprint=boundary_fingerprint,
                image_version=resolved_image_id,
                build_request=build_request,
                command=_RUN_TASK_COMMAND,
                diag_dir=diag_dir,
                replace_container=replace_container,
                exec_environment=exec_env,
            )
        except OrchestrationError as exc:
            persist_failed_run_evidence(
                candidate_root,
                run_id=run_id,
                candidate_rel=candidate_rel,
                error_code=exc.error_code,
                node_id=node_id,
                test_entity_id=test_entity_id,
                image_digest=resolved_image_id,
                container_id=exc.container_id,
                execution_state=exc.execution_state,
                exec_exit_code=exc.exit_code,
                execution_phase=exc.execution_phase,
                side_effect_possible=exc.side_effect_possible,
                secondary_execution_phase=exc.secondary_execution_phase,
                secondary_error_code=exc.secondary_error_code,
                secondary_detail=exc.secondary_detail,
                start_cleanup_failure=exc.start_cleanup_failure,
                resource_may_remain=exc.resource_may_remain,
                secondary_diagnostics=exc.secondary_diagnostics,
                detail=str(exc.detail),
            )
            logger.info(
                "container_test_failed",
                extra={
                    "action": "container_test_failed",
                    "status": "failed",
                    "detail": {"run_id": run_id, "node_id": node_id, "error_code": exc.error_code},
                },
            )
            raise AgentContainerError(
                exc.error_code,
                detail={**exc.detail, "run_id": run_id, "node_id": node_id, "container_id": exc.container_id},
            ) from exc

        try:
            envelope = parse_control_envelope_from_exec_output(
                result.exec_stdout,
                expected_run_id=run_id,
                expected_candidate_rel=candidate_rel,
            )
            exec_exit_code = int(result.exit_code) if result.exit_code is not None else None
            evidence = accept_control_evidence(
                envelope,
                host=HostAttestationContext(
                    container_id=result.container_id,
                    node_id=node_id,
                    test_entity_id=test_entity_id,
                    image_digest=resolved_image_id,
                    exec_exit_code=exec_exit_code,
                    execution_state=EXECUTION_EXITED,
                ),
                expected_run_id=run_id,
                expected_candidate_rel=candidate_rel,
                expected_node_id=node_id,
                expected_test_entity_id=test_entity_id,
                expected_container_id=result.container_id,
            )
        except ControlEnvelopeError as exc:
            exec_exit_code = int(result.exit_code) if result.exit_code is not None else None
            execution_state = EXECUTION_EXITED if exec_exit_code is not None else EXECUTION_STARTED_UNKNOWN
            persist_failed_run_evidence(
                candidate_root,
                run_id=run_id,
                candidate_rel=candidate_rel,
                error_code=exc.error_code,
                node_id=node_id,
                test_entity_id=test_entity_id,
                image_digest=resolved_image_id,
                container_id=result.container_id,
                execution_state=execution_state,
                exec_exit_code=exec_exit_code,
                execution_phase=EXECUTION_PHASE_FINALIZE,
                side_effect_possible=True,
                detail=exc.detail,
            )
            logger.info(
                "container_control_envelope_rejected",
                extra={
                    "action": "container_control_envelope_rejected",
                    "status": "rejected",
                    "detail": {
                        "run_id": run_id,
                        "candidate_rel": candidate_rel,
                        "error_code": exc.error_code,
                        "detail": exc.detail,
                    },
                },
            )
            raise AgentContainerError(
                exc.error_code,
                detail={"run_id": run_id, "candidate_rel": candidate_rel, "detail": exc.detail},
            ) from exc

        persist_control_evidence(candidate_root, evidence)
        manifest: dict[str, Any] = (evidence.get("envelope") or {}).get("manifest") or {}

        logger.info(
            "container_test_completed",
            extra={
                "action": "container_test_completed",
                "status": "completed",
                "detail": {
                    "run_id": run_id,
                    "node_id": node_id,
                    "container_id": result.container_id,
                    "reused": result.reused,
                    "candidate_rel": candidate_rel,
                },
            },
        )
        return {
            "container_id": result.container_id,
            "container_status": result.status,
            "container_health": result.health,
            "container_reused": result.reused,
            "exit_code": result.exit_code,
            "logs_summary": result.logs_summary[:500],
            "diagnostic_path": str(diag_dir),
            "manifest": manifest,
            "test_results": manifest.get("results") or [],
            "boundary_fingerprint": boundary_fingerprint,
            "image_version": image_version,
            "candidate_rel": candidate_rel,
        }
