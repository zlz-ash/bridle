"""Contracts for the exceptional provider wall-clock watchdog."""
from __future__ import annotations

from bridle.agent.tools.budget import ToolBudgetLimits, ToolBudgetTracker, budget_for_node_minutes


class TestToolBudgetTracker:
    def test_round_and_tool_usage_do_not_terminate_normal_work(self) -> None:
        tracker = ToolBudgetTracker(
            ToolBudgetLimits(max_wall_seconds=300.0),
            start_time=100.0,
        )
        tracker.sync_wall_clock = lambda: None

        for _ in range(500):
            assert tracker.check_before_round() is None
            tracker.begin_round()
            assert tracker.check_before_tool_call() is None
            tracker.record_tool_call(tool_name="run_command", args_summary="echo ok")

        assert tracker.usage.rounds_used == 500
        assert tracker.usage.tool_calls_used == 500

    def test_report_contains_only_wall_limit(self) -> None:
        tracker = ToolBudgetTracker(ToolBudgetLimits(max_wall_seconds=9.5))
        report = tracker.build_exhausted_report("wall_seconds")

        assert report["budget"]["type"] == "wall_seconds"
        assert report["budget"]["limits"] == {"max_wall_seconds": 9.5}


class TestBudgetForNodeMinutes:
    def test_scales_only_wall_watchdog(self) -> None:
        assert budget_for_node_minutes(1) == {"max_wall_seconds": 60.0}
        assert budget_for_node_minutes(30) == {"max_wall_seconds": 360.0}
        assert budget_for_node_minutes(300) == {"max_wall_seconds": 1800.0}

    def test_legacy_env_limits_are_ignored(self, monkeypatch) -> None:
        monkeypatch.setenv("BRIDLE_DEEPSEEK_MAX_TOOL_ROUNDS", "1")
        monkeypatch.setenv("BRIDLE_DEEPSEEK_MAX_TOOL_CALLS", "1")
        monkeypatch.setenv("BRIDLE_DEEPSEEK_MAX_WALL_SECONDS", "9.5")

        assert budget_for_node_minutes(30) == {"max_wall_seconds": 9.5}
