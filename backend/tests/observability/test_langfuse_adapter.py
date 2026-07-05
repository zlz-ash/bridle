"""Tests for Langfuse v4 adapter with explicit parent handle chain."""
from __future__ import annotations

import pytest

from bridle.observability.config import ObservabilityConfig
from bridle.observability.context import current_active_langfuse_trace
from bridle.observability.langfuse_adapter import LangfuseObservabilityAdapter
from bridle.observability.schema import GenerationRecord, ToolCallRecord

from .fake_langfuse import FakeLangfuse


def _config() -> ObservabilityConfig:
    return ObservabilityConfig(
        enabled=True,
        provider="langfuse",
        langfuse_public_key="pk",
        langfuse_secret_key="sk",
        langfuse_host="https://langfuse.example",
    )


class TestLangfuseAdapter:
    def test_start_trace_uses_client_start_observation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake_client_holder: dict[str, FakeLangfuse] = {}

        def _factory(**kwargs: object) -> FakeLangfuse:
            client = FakeLangfuse(**kwargs)  # type: ignore[arg-type]
            fake_client_holder["client"] = client
            return client

        monkeypatch.setattr("bridle.observability.langfuse_adapter.Langfuse", _factory)
        adapter = LangfuseObservabilityAdapter(_config())
        handle = adapter.start_trace("node_agent.run", session_id="s1", run_id="r1")
        handle.end(status="completed")

        client = fake_client_holder["client"]
        assert len(client.root_starts) == 2  # probe + trace
        trace_call = client.root_starts[-1]
        assert trace_call["as_type"] == "span"
        assert trace_call["name"] == "node_agent.run"
        assert trace_call["metadata"] == {"session_id": "s1", "run_id": "r1"}
        assert handle.trace_id.startswith("trace-node_agent.run")
        assert current_active_langfuse_trace() is None

    def test_start_span_attaches_child_to_active_root(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("bridle.observability.langfuse_adapter.Langfuse", FakeLangfuse)
        adapter = LangfuseObservabilityAdapter(_config())
        trace = adapter.start_trace("node_agent.run", run_id="r1")
        span = adapter.start_span("inner.step", phase="test")
        span.end(status="completed")
        trace.end(status="completed")

        child = adapter._client.child_starts[-1]  # type: ignore[attr-defined]
        assert child["name"] == "inner.step"
        assert child["parent_id"] == trace._trace.id  # type: ignore[attr-defined]

    def test_start_span_without_trace_creates_orphan_root_first(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("bridle.observability.langfuse_adapter.Langfuse", FakeLangfuse)
        adapter = LangfuseObservabilityAdapter(_config())
        span = adapter.start_span("solo", run_id="r1")
        span.end(status="completed")

        client = adapter._client  # type: ignore[attr-defined]
        orphan_roots = [c for c in client.root_starts if c["name"] == "orphan"]
        assert orphan_roots, "expected orphan root trace before span"
        child = client.child_starts[-1]
        assert child["name"] == "solo"

    def test_record_generation_uses_parent_start_observation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("bridle.observability.langfuse_adapter.Langfuse", FakeLangfuse)
        adapter = LangfuseObservabilityAdapter(_config())
        root = adapter.start_trace("node_agent.run", run_id="r1")
        adapter.record_generation(
            GenerationRecord(
                name="model.generation",
                model="deepseek-chat",
                input_summary={"messages_count": 2},
                output_summary={"finish_reason": "stop"},
                metadata={"run_id": "r1"},
                usage={"total_tokens": 42},
                duration_ms=100,
                status="completed",
            )
        )
        root.end(status="completed")

        gen = adapter._client.child_starts[-1]  # type: ignore[attr-defined]
        assert gen["as_type"] == "generation"
        assert gen["parent_id"] == root._trace.id  # type: ignore[attr-defined]
        assert gen["model"] == "deepseek-chat"
        assert gen["metadata"]["usage"] == {"total_tokens": 42}

    def test_record_tool_call_creates_span_event_and_updates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("bridle.observability.langfuse_adapter.Langfuse", FakeLangfuse)
        adapter = LangfuseObservabilityAdapter(_config())
        root = adapter.start_trace("node_agent.run", run_id="r1")
        adapter.record_tool_call(
            ToolCallRecord(
                tool_name="read",
                input_summary={"path": "a.py"},
                output_summary={"status": "completed"},
                duration_ms=50,
                status="completed",
                metadata={"run_id": "r1"},
            )
        )
        root.end(status="completed")

        client = adapter._client  # type: ignore[attr-defined]
        tool_spans = [obs for obs in client.observations if obs.name == "tool.read"]
        assert len(tool_spans) == 1
        tool_span = tool_spans[0]
        assert tool_span.parent is root._trace  # type: ignore[attr-defined]
        assert tool_span.event_calls[0]["name"] == "tool.result"
        assert tool_span.update_calls[0]["metadata"]["status"] == "completed"
        assert tool_span.end_calls

    def test_incompatible_sdk_raises_runtime_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _broken(**kwargs: object) -> FakeLangfuse:
            return FakeLangfuse(omit_methods=frozenset({"start_observation"}), **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr("bridle.observability.langfuse_adapter.Langfuse", _broken)
        with pytest.raises(RuntimeError, match="langfuse SDK incompatible: missing start_observation"):
            LangfuseObservabilityAdapter(_config())
