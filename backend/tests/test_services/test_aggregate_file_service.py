"""Tests for aggregate contribution merging."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from bridle.engine.aggregate_strategy import AggregateMergeStrategy
from bridle.services.aggregate_file_service import AggregateFileService


class TestAggregateFileService:
    def test_merges_json_list_contributions_in_declared_order(self, test_workspace: Path) -> None:
        contribution_dir = test_workspace / ".bridle" / "aggregate" / "src" / "router.py"
        contribution_dir.mkdir(parents=True, exist_ok=True)
        (contribution_dir / "node-a.json").write_text(
            json.dumps({"items": [{"path": "/a", "handler": "a"}]}),
            encoding="utf-8",
        )
        (contribution_dir / "node-b.json").write_text(
            json.dumps({"items": [{"path": "/b", "handler": "b"}]}),
            encoding="utf-8",
        )

        result = AggregateFileService(test_workspace).merge_json_list(
            aggregate_target="src/router.json",
            contribution_paths=[
                ".bridle/aggregate/src/router.py/node-a.json",
                ".bridle/aggregate/src/router.py/node-b.json",
            ],
            unique_key="path",
        )

        assert result["status"] == "merged"
        merged = json.loads((test_workspace / "src" / "router.json").read_text(encoding="utf-8"))
        assert merged == [{"path": "/a", "handler": "a"}, {"path": "/b", "handler": "b"}]

    def test_rejects_duplicate_unique_key(self, test_workspace: Path) -> None:
        contribution_dir = test_workspace / ".bridle" / "aggregate" / "src" / "router.py"
        contribution_dir.mkdir(parents=True, exist_ok=True)
        (contribution_dir / "node-a.json").write_text(json.dumps({"items": [{"path": "/same"}]}), encoding="utf-8")
        (contribution_dir / "node-b.json").write_text(json.dumps({"items": [{"path": "/same"}]}), encoding="utf-8")

        with pytest.raises(ValueError, match="duplicate aggregate item"):
            AggregateFileService(test_workspace).merge_json_list(
                aggregate_target="src/router.json",
                contribution_paths=[
                    ".bridle/aggregate/src/router.py/node-a.json",
                    ".bridle/aggregate/src/router.py/node-b.json",
                ],
                unique_key="path",
            )

    def test_rejects_out_of_workspace_contribution_path(self, test_workspace: Path) -> None:
        with pytest.raises(ValueError, match="workspace-relative"):
            AggregateFileService(test_workspace).merge_json_list(
                aggregate_target="src/router.json",
                contribution_paths=["../outside.json"],
                unique_key="path",
            )

    def test_merge_order_is_deterministic_when_requested(self, test_workspace: Path) -> None:
        contribution_dir = test_workspace / ".bridle" / "aggregate" / "src" / "router.py"
        contribution_dir.mkdir(parents=True, exist_ok=True)
        (contribution_dir / "node-b.json").write_text(
            json.dumps({"items": [{"path": "/b"}, {"path": "/a"}]}),
            encoding="utf-8",
        )
        (contribution_dir / "node-a.json").write_text(
            json.dumps({"items": [{"path": "/c"}]}),
            encoding="utf-8",
        )

        AggregateFileService(test_workspace).merge_json_list(
            aggregate_target="src/router.json",
            contribution_paths=[
                ".bridle/aggregate/src/router.py/node-b.json",
                ".bridle/aggregate/src/router.py/node-a.json",
            ],
            unique_key="path",
            sort_key="path",
        )

        merged = json.loads((test_workspace / "src" / "router.json").read_text(encoding="utf-8"))
        assert [item["path"] for item in merged] == ["/a", "/b", "/c"]


class TestAggregateMergeStrategy:
    def test_same_contributions_different_order_produces_same_output(self, test_workspace: Path) -> None:
        contribution_dir = test_workspace / ".bridle" / "aggregate" / "src" / "router.py"
        contribution_dir.mkdir(parents=True, exist_ok=True)
        (contribution_dir / "node-a.json").write_text(
            json.dumps({"items": [{"path": "/a", "handler": "a"}]}),
            encoding="utf-8",
        )
        (contribution_dir / "node-b.json").write_text(
            json.dumps({"items": [{"path": "/b", "handler": "b"}]}),
            encoding="utf-8",
        )

        strategy = AggregateMergeStrategy(
            aggregate_target="src/router.json",
            merge_strategy="json_list",
            unique_key="path",
            sort_key="path",
            duplicate_policy="reject",
        )
        service = AggregateFileService(test_workspace)

        result_a = service.merge_with_strategy(
            strategy=strategy,
            contribution_paths=[
                ".bridle/aggregate/src/router.py/node-a.json",
                ".bridle/aggregate/src/router.py/node-b.json",
            ],
        )
        output_a = json.loads((test_workspace / "src" / "router.json").read_text(encoding="utf-8"))

        result_b = service.merge_with_strategy(
            strategy=strategy,
            contribution_paths=[
                ".bridle/aggregate/src/router.py/node-b.json",
                ".bridle/aggregate/src/router.py/node-a.json",
            ],
        )
        output_b = json.loads((test_workspace / "src" / "router.json").read_text(encoding="utf-8"))

        assert output_a == output_b
        assert [item["path"] for item in output_a] == ["/a", "/b"]

    def test_rejects_unknown_merge_strategy(self, test_workspace: Path) -> None:
        strategy = AggregateMergeStrategy(
            aggregate_target="src/router.json",
            merge_strategy="unknown_strategy",
            unique_key="path",
            duplicate_policy="reject",
        )

        with pytest.raises(ValueError, match="Unknown merge strategy"):
            AggregateFileService(test_workspace).merge_with_strategy(
                strategy=strategy,
                contribution_paths=[],
            )

    def test_duplicate_policy_reject(self, test_workspace: Path) -> None:
        contribution_dir = test_workspace / ".bridle" / "aggregate" / "src" / "router.py"
        contribution_dir.mkdir(parents=True, exist_ok=True)
        (contribution_dir / "node-a.json").write_text(json.dumps({"items": [{"path": "/same"}]}), encoding="utf-8")
        (contribution_dir / "node-b.json").write_text(json.dumps({"items": [{"path": "/same"}]}), encoding="utf-8")

        strategy = AggregateMergeStrategy(
            aggregate_target="src/router.json",
            merge_strategy="json_list",
            unique_key="path",
            duplicate_policy="reject",
        )

        with pytest.raises(ValueError, match="duplicate aggregate item"):
            AggregateFileService(test_workspace).merge_with_strategy(
                strategy=strategy,
                contribution_paths=[
                    ".bridle/aggregate/src/router.py/node-a.json",
                    ".bridle/aggregate/src/router.py/node-b.json",
                ],
            )

    def test_duplicate_policy_last_wins(self, test_workspace: Path) -> None:
        contribution_dir = test_workspace / ".bridle" / "aggregate" / "src" / "router.py"
        contribution_dir.mkdir(parents=True, exist_ok=True)
        (contribution_dir / "node-a.json").write_text(
            json.dumps({"items": [{"path": "/same", "handler": "first"}]}),
            encoding="utf-8",
        )
        (contribution_dir / "node-b.json").write_text(
            json.dumps({"items": [{"path": "/same", "handler": "last"}]}),
            encoding="utf-8",
        )

        strategy = AggregateMergeStrategy(
            aggregate_target="src/router.json",
            merge_strategy="json_list",
            unique_key="path",
            duplicate_policy="last_wins",
        )

        result = AggregateFileService(test_workspace).merge_with_strategy(
            strategy=strategy,
            contribution_paths=[
                ".bridle/aggregate/src/router.py/node-a.json",
                ".bridle/aggregate/src/router.py/node-b.json",
            ],
        )

        merged = json.loads((test_workspace / "src" / "router.json").read_text(encoding="utf-8"))
        assert len(merged) == 1
        assert merged[0]["handler"] == "last"

    def test_strategy_is_logged(self, test_workspace: Path, caplog) -> None:
        contribution_dir = test_workspace / ".bridle" / "aggregate" / "src" / "router.py"
        contribution_dir.mkdir(parents=True, exist_ok=True)
        (contribution_dir / "node-a.json").write_text(
            json.dumps({"items": [{"path": "/a", "handler": "a"}]}),
            encoding="utf-8",
        )

        strategy = AggregateMergeStrategy(
            aggregate_target="src/router.json",
            merge_strategy="json_list",
            unique_key="path",
            duplicate_policy="reject",
        )

        with caplog.at_level("INFO"):
            AggregateFileService(test_workspace).merge_with_strategy(
                strategy=strategy,
                contribution_paths=[".bridle/aggregate/src/router.py/node-a.json"],
            )

        assert any("aggregate_strategy_validated" in r.message for r in caplog.records)

    def test_contribution_schema_rejects_missing_required_field(self, test_workspace: Path) -> None:
        contribution_dir = test_workspace / ".bridle" / "aggregate" / "src" / "router.py"
        contribution_dir.mkdir(parents=True, exist_ok=True)
        (contribution_dir / "node-a.json").write_text(
            json.dumps({"items": [{"path": "/a"}]}),
            encoding="utf-8",
        )

        strategy = AggregateMergeStrategy(
            aggregate_target="src/router.json",
            merge_strategy="json_list",
            unique_key="path",
            contribution_schema={"path": "str", "handler": "str"},
            duplicate_policy="reject",
        )

        with pytest.raises(ValueError, match="missing required field.*handler"):
            AggregateFileService(test_workspace).merge_with_strategy(
                strategy=strategy,
                contribution_paths=[".bridle/aggregate/src/router.py/node-a.json"],
            )

    def test_contribution_schema_allows_items_with_all_required_fields(self, test_workspace: Path) -> None:
        contribution_dir = test_workspace / ".bridle" / "aggregate" / "src" / "router.py"
        contribution_dir.mkdir(parents=True, exist_ok=True)
        (contribution_dir / "node-a.json").write_text(
            json.dumps({"items": [{"path": "/a", "handler": "a"}]}),
            encoding="utf-8",
        )

        strategy = AggregateMergeStrategy(
            aggregate_target="src/router.json",
            merge_strategy="json_list",
            unique_key="path",
            contribution_schema={"path": "str", "handler": "str"},
            duplicate_policy="reject",
        )

        result = AggregateFileService(test_workspace).merge_with_strategy(
            strategy=strategy,
            contribution_paths=[".bridle/aggregate/src/router.py/node-a.json"],
        )

        assert result["status"] == "merged"

    def test_validation_commands_failure_rejects_merge(self, test_workspace: Path) -> None:
        contribution_dir = test_workspace / ".bridle" / "aggregate" / "src" / "router.py"
        contribution_dir.mkdir(parents=True, exist_ok=True)
        (contribution_dir / "node-a.json").write_text(
            json.dumps({"items": [{"path": "/a", "handler": "a"}]}),
            encoding="utf-8",
        )

        strategy = AggregateMergeStrategy(
            aggregate_target="src/router.json",
            merge_strategy="json_list",
            unique_key="path",
            validation_commands=["python -c \"raise SystemExit(1)\""],
            duplicate_policy="reject",
        )

        with pytest.raises(ValueError, match="validation_command_failed"):
            AggregateFileService(test_workspace).merge_with_strategy(
                strategy=strategy,
                contribution_paths=[".bridle/aggregate/src/router.py/node-a.json"],
            )

    def test_validation_command_reads_candidate_path(self, test_workspace: Path) -> None:
        contribution_dir = test_workspace / ".bridle" / "aggregate" / "src" / "router.py"
        contribution_dir.mkdir(parents=True, exist_ok=True)
        (contribution_dir / "node-a.json").write_text(
            json.dumps({"items": [{"path": "/a", "handler": "a"}]}),
            encoding="utf-8",
        )
        target = test_workspace / "src" / "router.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("not json", encoding="utf-8")

        strategy = AggregateMergeStrategy(
            aggregate_target="src/router.json",
            merge_strategy="json_list",
            unique_key="path",
            validation_commands=[
                f"{sys.executable} -c \"import json,sys; data=json.load(open(sys.argv[1], encoding='utf-8')); assert data[0]['path']=='/a'\" {{candidate}}"
            ],
            duplicate_policy="reject",
        )

        AggregateFileService(test_workspace).merge_with_strategy(
            strategy=strategy,
            contribution_paths=[".bridle/aggregate/src/router.py/node-a.json"],
        )

        assert json.loads(target.read_text(encoding="utf-8")) == [{"path": "/a", "handler": "a"}]

    def test_merge_json_list_delegates_to_strategy(self, test_workspace: Path) -> None:
        contribution_dir = test_workspace / ".bridle" / "aggregate" / "src" / "router.py"
        contribution_dir.mkdir(parents=True, exist_ok=True)
        (contribution_dir / "node-a.json").write_text(
            json.dumps({"items": [{"path": "/a", "handler": "a"}]}),
            encoding="utf-8",
        )

        result = AggregateFileService(test_workspace).merge_json_list(
            aggregate_target="src/router.json",
            contribution_paths=[".bridle/aggregate/src/router.py/node-a.json"],
            unique_key="path",
        )

        assert result["status"] == "merged"
        merged = json.loads((test_workspace / "src" / "router.json").read_text(encoding="utf-8"))
        assert merged == [{"path": "/a", "handler": "a"}]
