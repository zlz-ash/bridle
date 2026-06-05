"""Tests for planner_template."""
from __future__ import annotations

from bridle.engine.planner_template import PLANNER_SYSTEM_TEMPLATE, build_planner_messages


def test_planner_template_non_empty() -> None:
    assert "planning agent" in PLANNER_SYSTEM_TEMPLATE.lower()
    assert "planimportschema" in PLANNER_SYSTEM_TEMPLATE.lower()
    assert "tests" in PLANNER_SYSTEM_TEMPLATE.lower()
    assert "MUST be a non-empty list" in PLANNER_SYSTEM_TEMPLATE


def test_build_planner_messages_empty_history() -> None:
    messages = build_planner_messages([], {"files": []})
    assert len(messages) == 2
    assert messages[0]["role"] == "system"
    assert messages[1]["role"] == "system"
    assert "Workspace overview" in messages[1]["content"]


def test_build_planner_messages_with_user_turn() -> None:
    messages = build_planner_messages([{"role": "user", "content": "hi"}], {})
    assert len(messages) == 3
    assert messages[-1] == {"role": "user", "content": "hi"}
