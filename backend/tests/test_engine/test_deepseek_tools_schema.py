"""Tests for DeepSeek tools schema builder."""
from __future__ import annotations

from bridle.engine.deepseek_tools_schema import build_deepseek_tools, tool_names


class TestDeepSeekToolsSchema:
    def test_contains_four_v1_tools(self) -> None:
        tools = build_deepseek_tools(strict=False)
        names = tool_names(tools)
        assert names == {
            "read_allowed_file",
            "propose_file_patch",
            "run_allowed_tests",
            "report_blocked",
        }

    def test_additional_properties_false(self) -> None:
        tools = build_deepseek_tools(strict=False)
        for tool in tools:
            params = tool["function"]["parameters"]
            assert params.get("additionalProperties") is False

    def test_strict_false_no_strict_flag(self) -> None:
        tools = build_deepseek_tools(strict=False)
        for tool in tools:
            assert "strict" not in tool["function"]

    def test_strict_true_adds_strict_flag(self) -> None:
        tools = build_deepseek_tools(strict=True)
        for tool in tools:
            assert tool["function"].get("strict") is True

    def test_no_forbidden_schema_fields(self) -> None:
        import json

        blob = json.dumps(build_deepseek_tools(strict=False))
        for forbidden in ("minLength", "maxLength", "minItems", "maxItems"):
            assert forbidden not in blob
