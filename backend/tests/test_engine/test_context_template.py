"""Tests for context template builder."""
from __future__ import annotations

import json

import pytest

from bridle.engine.agent_tool_registry import AgentToolRegistry
from bridle.engine.context_template import ContextTemplateBuilder
from bridle.engine.context_types import ContextPayload
from bridle.schemas.proposal import AgentContext


def _minimal_ctx(**overrides) -> AgentContext:
    base = {
        "instruction": "Implement feature X",
        "node": {"id": "n1", "title": "Node 1", "goal": "Do X"},
        "allowed_files": ["src/a.py"],
        "tests": ["pytest"],
        "metrics": {},
        "constraints": {},
        "review_checks": [],
        "expected_outputs": {},
        "accessible_context": {},
        "tool_capabilities": {},
    }
    base.update(overrides)
    return AgentContext(**base)


class TestContextTemplateBuilder:
    def test_system_message_contains_architecture(self) -> None:
        ctx = _minimal_ctx()
        builder = ContextTemplateBuilder(ctx)
        messages = builder.build_messages()
        system = messages[0]
        assert system["role"] == "system"
        content = system["content"]
        assert "agent" in content.lower()
        assert "implement" in content.lower() or "execute" in content.lower()
        assert "test" in content.lower()

    def test_system_message_describes_execution_order(self) -> None:
        ctx = _minimal_ctx()
        builder = ContextTemplateBuilder(ctx)
        messages = builder.build_messages()
        content = messages[0]["content"]
        assert "implement" in content.lower()
        assert "metric" in content.lower() or "indicator" in content.lower()
        assert "test" in content.lower()

    def test_user_payload_contains_context_layers(self) -> None:
        ctx = _minimal_ctx()
        builder = ContextTemplateBuilder(ctx)
        messages = builder.build_messages()
        user_msg = messages[1]
        assert user_msg["role"] == "user"
        payload = json.loads(user_msg["content"])
        assert "instruction" in payload
        assert "node" in payload
        assert "short_term_memory" in payload
        assert "tool_context" in payload
        assert "long_term_memory" in payload
        assert "rag" in payload

    def test_user_payload_preserves_original_fields(self) -> None:
        ctx = _minimal_ctx()
        builder = ContextTemplateBuilder(ctx)
        messages = builder.build_messages()
        payload = json.loads(messages[1]["content"])
        assert payload["instruction"] == "Implement feature X"
        assert payload["node"]["id"] == "n1"
        assert payload["allowed_files"] == ["src/a.py"]

    def test_long_term_memory_default_empty(self) -> None:
        ctx = _minimal_ctx()
        builder = ContextTemplateBuilder(ctx)
        messages = builder.build_messages()
        payload = json.loads(messages[1]["content"])
        assert payload["long_term_memory"] == {}

    def test_rag_default_empty(self) -> None:
        ctx = _minimal_ctx()
        builder = ContextTemplateBuilder(ctx)
        messages = builder.build_messages()
        payload = json.loads(messages[1]["content"])
        assert payload["rag"] == {}

    def test_short_term_memory_default_empty(self) -> None:
        ctx = _minimal_ctx()
        builder = ContextTemplateBuilder(ctx)
        messages = builder.build_messages()
        payload = json.loads(messages[1]["content"])
        assert payload["short_term_memory"] == []

    def test_tool_context_default_not_empty(self) -> None:
        ctx = _minimal_ctx()
        builder = ContextTemplateBuilder(ctx)
        messages = builder.build_messages()
        payload = json.loads(messages[1]["content"])
        assert len(payload["tool_context"]) == 5

    def test_tool_context_default_has_five_descriptors(self) -> None:
        ctx = _minimal_ctx()
        builder = ContextTemplateBuilder(ctx)
        payload = builder.build_payload()
        assert len(payload.tool_context) == 5
        names = [d["name"] for d in payload.tool_context]
        assert "read_allowed_file" in names
        assert "propose_file_patch" in names
        assert "run_allowed_tests" in names
        assert "report_blocked" in names
        assert "child_agent_result_summary" in names

    def test_tool_context_default_each_descriptor_has_required_fields(self) -> None:
        ctx = _minimal_ctx()
        builder = ContextTemplateBuilder(ctx)
        payload = builder.build_payload()
        for d in payload.tool_context:
            assert "name" in d
            assert "purpose" in d
            assert "when_to_use" in d
            assert "input_summary" in d
            assert "output_summary" in d
            assert "constraints" in d
            assert "reserved" in d

    def test_tool_context_explicit_override_replaces_default(self) -> None:
        ctx = _minimal_ctx()
        custom = [{"name": "custom_tool", "purpose": "Custom"}]
        builder = ContextTemplateBuilder(ctx, tool_context=custom)
        payload = builder.build_payload()
        assert len(payload.tool_context) == 1
        assert payload.tool_context[0]["name"] == "custom_tool"

    def test_build_payload_returns_structured_object(self) -> None:
        ctx = _minimal_ctx()
        builder = ContextTemplateBuilder(ctx)
        payload = builder.build_payload()
        assert isinstance(payload, ContextPayload)
        assert payload.instruction == "Implement feature X"
        assert payload.long_term_memory == {}
        assert payload.rag == {}

    def test_proposal_output_format_in_system(self) -> None:
        ctx = _minimal_ctx()
        builder = ContextTemplateBuilder(ctx)
        messages = builder.build_messages()
        content = messages[0]["content"]
        assert "summary" in content
        assert "file_patches" in content
        assert "tests_to_run" in content


class TestContextTemplateBuilderWithMemory:
    def test_short_term_memory_injected(self) -> None:
        ctx = _minimal_ctx()
        memory = [{"role": "user", "content": "previous instruction"}]
        builder = ContextTemplateBuilder(ctx, short_term_memory=memory)
        messages = builder.build_messages()
        payload = json.loads(messages[1]["content"])
        assert len(payload["short_term_memory"]) == 1
        assert payload["short_term_memory"][0]["content"] == "previous instruction"

    def test_tool_context_injected(self) -> None:
        ctx = _minimal_ctx()
        tool_ctx = [{"name": "read_allowed_file", "purpose": "Read files"}]
        builder = ContextTemplateBuilder(ctx, tool_context=tool_ctx)
        messages = builder.build_messages()
        payload = json.loads(messages[1]["content"])
        assert len(payload["tool_context"]) == 1
        assert payload["tool_context"][0]["name"] == "read_allowed_file"

    def test_child_agent_results_default_empty(self) -> None:
        ctx = _minimal_ctx()
        builder = ContextTemplateBuilder(ctx)
        payload = builder.build_payload()
        assert payload.child_agent_results == []

    def test_child_agent_results_injected(self) -> None:
        ctx = _minimal_ctx()
        results = [
            {
                "node_id": "n2",
                "status": "completed",
                "result_summary": "Done",
                "test_summary": "2/2 passed",
                "metrics_summary": "1/1 ok",
            },
        ]
        builder = ContextTemplateBuilder(ctx, child_agent_results=results)
        messages = builder.build_messages()
        payload = json.loads(messages[1]["content"])
        assert len(payload["child_agent_results"]) == 1
        assert payload["child_agent_results"][0]["node_id"] == "n2"
        assert payload["child_agent_results"][0]["status"] == "completed"

    def test_child_agent_results_pending_node(self) -> None:
        ctx = _minimal_ctx()
        results = [
            {"node_id": "n3", "status": "pending", "result_summary": "", "test_summary": "", "metrics_summary": ""},
        ]
        builder = ContextTemplateBuilder(ctx, child_agent_results=results)
        payload = builder.build_payload()
        assert payload.child_agent_results[0]["status"] == "pending"

    def test_child_agent_results_only_summary_no_raw(self) -> None:
        ctx = _minimal_ctx()
        results = [
            {
                "node_id": "n2",
                "status": "completed",
                "result_summary": "Done",
                "test_summary": "2/2 passed",
                "metrics_summary": "1/1 ok",
                "raw_patch": "should not appear",
                "full_log": "should not appear",
            },
        ]
        builder = ContextTemplateBuilder(ctx, child_agent_results=results)
        messages = builder.build_messages()
        payload_str = messages[1]["content"]
        assert "raw_patch" not in payload_str
        assert "full_log" not in payload_str


class TestContextTemplateBuilderLogEvents:
    def test_build_emits_context_template_built(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        import logging

        caplog.set_level(logging.INFO, logger="bridle")
        ctx = _minimal_ctx()
        builder = ContextTemplateBuilder(ctx, run_id="r1", node_id="n1")
        builder.build_messages()
        events = [
            r for r in caplog.records
            if getattr(r, "action", None) == "context_template_built"
        ]
        assert len(events) == 1
        detail = getattr(events[0], "detail", {})
        assert detail.get("layer_count") is not None

    def test_build_with_tool_context_emits_tool_context_disclosed(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        import logging

        caplog.set_level(logging.INFO, logger="bridle")
        ctx = _minimal_ctx()
        descriptors = AgentToolRegistry.tool_descriptors()
        tool_dicts = [d.model_dump() for d in descriptors]
        builder = ContextTemplateBuilder(ctx, tool_context=tool_dicts, run_id="r1", node_id="n1")
        builder.build_messages()
        events = [
            r for r in caplog.records
            if getattr(r, "action", None) == "tool_context_disclosed"
        ]
        assert len(events) == 1
        detail = getattr(events[0], "detail", {})
        assert detail.get("tool_count") == 5
        assert "stdout" not in json.dumps(detail)
        assert "stderr" not in json.dumps(detail)

    def test_build_with_child_results_emits_child_agent_results_attached(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        import logging

        caplog.set_level(logging.INFO, logger="bridle")
        ctx = _minimal_ctx()
        results = [{"node_id": "n2", "status": "completed", "result_summary": "Done"}]
        builder = ContextTemplateBuilder(ctx, child_agent_results=results, run_id="r1", node_id="n1")
        builder.build_messages()
        events = [
            r for r in caplog.records
            if getattr(r, "action", None) == "child_agent_results_attached"
        ]
        assert len(events) == 1
        detail = getattr(events[0], "detail", {})
        assert detail.get("result_count") == 1
        assert "stdout" not in json.dumps(detail)
        assert "stderr" not in json.dumps(detail)
        assert "raw_patch" not in json.dumps(detail)


class TestToolContextDisclosure:
    def test_default_tool_context_has_five_entries(self) -> None:
        descriptors = AgentToolRegistry.tool_descriptors()
        assert len(descriptors) <= 5
        assert len(descriptors) == 5

    def test_each_descriptor_has_standard_fields(self) -> None:
        descriptors = AgentToolRegistry.tool_descriptors()
        for d in descriptors:
            assert d.name
            assert d.purpose
            assert d.when_to_use
            assert d.input_summary
            assert d.output_summary
            assert d.constraints

    def test_reserved_tool_not_in_deepseek_schema(self) -> None:
        from bridle.engine.deepseek_tools_schema import V1_TOOL_NAMES

        descriptors = AgentToolRegistry.tool_descriptors()
        reserved = [d for d in descriptors if d.reserved]
        deepseek_names = set(V1_TOOL_NAMES)
        for r in reserved:
            assert r.name not in deepseek_names

    def test_non_reserved_tools_match_deepseek_schema(self) -> None:
        from bridle.engine.deepseek_tools_schema import V1_TOOL_NAMES

        descriptors = AgentToolRegistry.tool_descriptors()
        non_reserved = {d.name for d in descriptors if not d.reserved}
        assert non_reserved == set(V1_TOOL_NAMES)

    def test_tool_context_does_not_leak_raw_results(self) -> None:
        descriptors = AgentToolRegistry.tool_descriptors()
        for d in descriptors:
            dumped = d.model_dump()
            assert "stdout" not in dumped
            assert "stderr" not in dumped
            assert "raw_result" not in dumped
            assert "arguments" not in dumped

    def test_builder_with_tool_context_from_registry(self) -> None:
        ctx = _minimal_ctx()
        descriptors = AgentToolRegistry.tool_descriptors()
        tool_dicts = [d.model_dump() for d in descriptors]
        builder = ContextTemplateBuilder(ctx, tool_context=tool_dicts)
        messages = builder.build_messages()
        payload = json.loads(messages[1]["content"])
        assert len(payload["tool_context"]) == 5
        assert payload["tool_context"][0]["name"] == "read_allowed_file"

    def test_tool_context_capped_at_five(self) -> None:
        descriptors = AgentToolRegistry.tool_descriptors()
        assert len(descriptors) <= 5
