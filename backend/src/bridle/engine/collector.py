"""Collector — gather evidence from run results."""
from __future__ import annotations

from bridle.models.node import NodeRecord


class Collector:
    """Collect evidence from command execution results."""

    @staticmethod
    def collect_test_evidence(node: NodeRecord, results: list[dict]) -> dict:
        """Collect test result evidence from execution results."""
        all_passed = all(r["exit_code"] == 0 for r in results)
        return {
            "evidence_type": "test_result",
            "content": {
                "total_commands": len(results),
                "passed": sum(1 for r in results if r["exit_code"] == 0),
                "failed": sum(1 for r in results if r["exit_code"] != 0),
                "results": [
                    {
                        "exit_code": r["exit_code"],
                        "duration_ms": r["duration_ms"],
                        "stdout_preview": r["stdout"][:500] if r["stdout"] else "",
                        "stderr_preview": r["stderr"][:500] if r["stderr"] else "",
                    }
                    for r in results
                ],
            },
            "status": "collected" if all_passed else "failed",
        }

    @staticmethod
    def collect_metric_evidence(node: NodeRecord, results: list[dict]) -> dict:
        """Collect metric evidence from execution results."""
        return {
            "evidence_type": "metric",
            "content": {
                "defined_metrics": node.metrics,
                "command_results": [
                    {"exit_code": r["exit_code"], "stdout": r["stdout"][:1000]}
                    for r in results
                ],
            },
            "status": "collected" if all(r["exit_code"] == 0 for r in results) else "failed",
        }

    @staticmethod
    def collect_log_evidence(results: list[dict]) -> dict:
        """Collect log evidence from execution results."""
        return {
            "evidence_type": "log",
            "content": {
                "logs": [
                    {"stdout": r["stdout"][:2000], "stderr": r["stderr"][:2000]}
                    for r in results
                ],
            },
            "status": "collected",
        }

    @staticmethod
    def collect_for_node(node: NodeRecord, results: list[dict]) -> list[dict]:
        """Collect all relevant evidence for a node based on its type."""
        evidences = []

        # Always collect logs
        evidences.append(Collector.collect_log_evidence(results))

        # Type-specific evidence
        if node.tests:
            evidences.append(Collector.collect_test_evidence(node, results))

        if node.node_type == "metric_validation" and node.metrics:
            evidences.append(Collector.collect_metric_evidence(node, results))

        return evidences
