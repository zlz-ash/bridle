"""Tests for ToolBudgetTracker boundary semantics."""
from __future__ import annotations

import pytest

from bridle.engine.tool_budget import ToolBudgetLimits, ToolBudgetTracker


class TestToolBudgetTracker:
    def test_allows_max_rounds_before_exhausting(self) -> None:
        tracker = ToolBudgetTracker(
            ToolBudgetLimits(max_rounds=3, max_tool_calls=100, max_wall_seconds=300.0)
        )
        for _ in range(3):
            assert tracker.check_before_round() is None
            tracker.begin_round()
        assert tracker.check_before_round() == "rounds"

    def test_allows_max_tool_calls_before_exhausting(self) -> None:
        tracker = ToolBudgetTracker(
            ToolBudgetLimits(max_rounds=100, max_tool_calls=2, max_wall_seconds=300.0)
        )
        assert tracker.check_before_tool_call() is None
        tracker.record_tool_call(tool_name="read_allowed_file", args_summary="path=src/a.py")
        assert tracker.check_before_tool_call() is None
        tracker.record_tool_call(tool_name="grep_code", args_summary="query=test")
        assert tracker.check_before_tool_call() == "tool_calls"
        assert tracker.usage.tool_calls_used == 2

    def test_last_tool_call_records_latest_tool(self) -> None:
        tracker = ToolBudgetTracker(
            ToolBudgetLimits(max_rounds=8, max_tool_calls=8, max_wall_seconds=300.0)
        )
        tracker.record_tool_call(tool_name="read_allowed_file", args_summary="path=src/a.py")
        tracker.record_tool_call(
            tool_name="run_allowed_tests",
            args_summary="commands=['pytest']",
        )
        assert tracker.last_tool_call is not None
        assert tracker.last_tool_call["tool_name"] == "run_allowed_tests"


class TestBudgetForNodeMinutes:
    @pytest.fixture(autouse=True)
    def _clean_budget_env(self, monkeypatch):
        for var in (
            "BRIDLE_DEEPSEEK_MAX_TOOL_ROUNDS",
            "BRIDLE_DEEPSEEK_MAX_TOOL_CALLS",
            "BRIDLE_DEEPSEEK_MAX_WALL_SECONDS",
        ):
            monkeypatch.delenv(var, raising=False)

    def test_default_minutes_yields_60_baseline(self) -> None:
        from bridle.engine.tool_budget import budget_for_node_minutes
        b = budget_for_node_minutes(None)
        assert b["max_rounds"] == 20
        assert b["max_tool_calls"] == 80
        assert b["max_wall_seconds"] == 720.0

    def test_clamps_to_lower_bound(self) -> None:
        from bridle.engine.tool_budget import budget_for_node_minutes
        b = budget_for_node_minutes(10)
        assert b["max_rounds"] == 8
        assert b["max_tool_calls"] == 32
        assert b["max_wall_seconds"] == 120.0

    def test_clamps_to_upper_bound(self) -> None:
        from bridle.engine.tool_budget import budget_for_node_minutes
        b = budget_for_node_minutes(300)
        assert b["max_rounds"] == 60
        assert b["max_tool_calls"] == 240
        assert b["max_wall_seconds"] == 1800.0

    def test_proportional_in_middle_range(self) -> None:
        from bridle.engine.tool_budget import budget_for_node_minutes
        b = budget_for_node_minutes(90)
        assert b["max_rounds"] == 30
        assert b["max_tool_calls"] == 120
        assert b["max_wall_seconds"] == 1080.0

    def test_env_var_override_takes_precedence(self, monkeypatch) -> None:
        from bridle.engine.tool_budget import budget_for_node_minutes
        monkeypatch.setenv("BRIDLE_DEEPSEEK_MAX_TOOL_ROUNDS", "3")
        monkeypatch.setenv("BRIDLE_DEEPSEEK_MAX_TOOL_CALLS", "5")
        monkeypatch.setenv("BRIDLE_DEEPSEEK_MAX_WALL_SECONDS", "9.5")
        b = budget_for_node_minutes(180)
        assert b["max_rounds"] == 3
        assert b["max_tool_calls"] == 5
        assert b["max_wall_seconds"] == 9.5

