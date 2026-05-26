"""Production entry: collect, apply, merge aggregate, and checkpoint after node container output."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from bridle.engine.aggregate_plan import load_aggregate_strategies, map_contributions_by_target
from bridle.engine.aggregate_strategy import AggregateMergeStrategy
from bridle.services.aggregate_file_service import AggregateFileService
from bridle.services.git_checkpoint_service import GitCheckpointService
from bridle.services.integration_service import IntegrationService

logger = logging.getLogger("bridle")


class NodeOutputIntegrationService:
    def __init__(self, workspace_root: str | Path) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self._integration = IntegrationService(self.workspace_root)
        self._aggregate = AggregateFileService(self.workspace_root)
        self._checkpoints = GitCheckpointService(self.workspace_root)

    def integrate_run(
        self,
        *,
        run_id: str,
        session_id: str,
        allowed_files: list[str],
        allowed_aggregate_paths: list[str],
        expected_baseline_revision: str,
        aggregate_strategies: list[AggregateMergeStrategy] | None = None,
    ) -> dict[str, Any]:
        strategies = aggregate_strategies or load_aggregate_strategies(self.workspace_root)
        snapshot_paths = list(allowed_files)
        snapshot_paths.extend(strategy.aggregate_target for strategy in strategies)
        begin_state = self._checkpoints.begin_integration(session_id, snapshot_paths=snapshot_paths)

        try:
            collected = self._integration.collect_node_output(
                run_id=run_id,
                allowed_files=allowed_files,
                allowed_aggregate_paths=allowed_aggregate_paths,
                expected_baseline_revision=expected_baseline_revision,
            )
            applied = self._integration.apply_workspace_outputs(
                run_id=run_id,
                allowed_files=allowed_files,
                expected_baseline_revision=expected_baseline_revision,
            )
            merge_results: list[dict[str, Any]] = []
            contributions_by_target = map_contributions_by_target(
                collected.get("aggregate_contributions_meta", []),
                allowed_paths=allowed_aggregate_paths,
            )
            if not contributions_by_target:
                contributions_by_target = map_contributions_by_target(
                    [
                        {"path": path, "aggregate_target": self._target_for_contribution(path, strategies)}
                        for path in collected.get("aggregate_contributions", [])
                    ],
                    allowed_paths=allowed_aggregate_paths,
                )
            for strategy in strategies:
                contribution_paths = contributions_by_target.get(strategy.aggregate_target, [])
                if not contribution_paths:
                    continue
                merge_results.append(
                    self._aggregate.merge_with_strategy(
                        strategy=strategy,
                        contribution_paths=contribution_paths,
                    )
                )
            commit_paths = list(allowed_files) + [s.aggregate_target for s in strategies]
            checkpoint = self._checkpoints.commit_after_integration(
                session_id,
                commit_paths=commit_paths,
            )
        except Exception as exc:
            try:
                self._checkpoints.rollback_integration(session_id)
            except Exception as rollback_exc:
                logger.info(
                    "node_output_integration_rollback_failed",
                    extra={
                        "action": "node_output_integration_rollback_failed",
                        "status": "failed",
                        "detail": {
                            "run_id": run_id,
                            "error": str(rollback_exc),
                            "original_error": str(exc),
                        },
                    },
                )
            logger.info(
                "node_output_integration_failed",
                extra={
                    "action": "node_output_integration_failed",
                    "status": "failed",
                    "detail": {"run_id": run_id, "error": str(exc), "begin": begin_state},
                },
            )
            raise

        logger.info(
            "node_output_integration_completed",
            extra={
                "action": "node_output_integration_completed",
                "status": "completed",
                "detail": {"run_id": run_id, "session_id": session_id},
            },
        )
        return {
            "status": "integrated",
            "collected": collected,
            "applied": applied,
            "merge_results": merge_results,
            "checkpoint": checkpoint,
            "begin_state": begin_state,
        }

    @staticmethod
    def _target_for_contribution(
        path: str,
        strategies: list[AggregateMergeStrategy],
    ) -> str:
        for strategy in strategies:
            prefix = strategy.aggregate_target.rstrip("/") + "/"
            if path == strategy.aggregate_target or path.startswith(prefix):
                return strategy.aggregate_target
        return ""
