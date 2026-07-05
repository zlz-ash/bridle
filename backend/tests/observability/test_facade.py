"""Tests for observability facade and adapters."""
from __future__ import annotations

import logging

import pytest

from bridle.observability.config import ObservabilityConfig
from bridle.observability.facade import ObservabilityFacade, _build_adapter, get_observability, reset_observability
from bridle.observability.noop_adapter import NoopObservabilityAdapter
from bridle.observability.schema import ObservabilityContext

from .fake_langfuse import FakeLangfuse


@pytest.fixture(autouse=True)
def _reset_obs() -> None:
    reset_observability()
    yield
    reset_observability()


class TestObservabilityFacade:
    def test_uses_noop_when_disabled(self) -> None:
        facade = ObservabilityFacade(ObservabilityConfig.disabled())
        assert isinstance(facade.adapter, NoopObservabilityAdapter)
        trace = facade.start_trace("node_agent.run", session_id="s1", run_id="r1")
        trace.end(status="completed")
        assert trace.trace_id == "noop"

    def test_delegates_to_langfuse_adapter_when_configured(self) -> None:
        from unittest.mock import MagicMock

        mock_adapter = MagicMock()
        mock_trace = MagicMock()
        mock_trace.trace_id = "lf-1"
        mock_adapter.start_trace.return_value = mock_trace

        facade = ObservabilityFacade(ObservabilityConfig.disabled(), adapter=mock_adapter)
        trace = facade.start_trace("node_agent.run", session_id="s1")
        trace.end(status="completed")

        mock_adapter.start_trace.assert_called_once()
        mock_trace.end.assert_called_once_with(status="completed")

    def test_swallows_adapter_errors(self) -> None:
        from unittest.mock import MagicMock

        mock_adapter = MagicMock()
        mock_adapter.start_trace.side_effect = RuntimeError("langfuse down")

        facade = ObservabilityFacade(ObservabilityConfig.disabled(), adapter=mock_adapter)
        trace = facade.start_trace("node_agent.run")
        assert trace.trace_id == "noop"
        facade.record_generation(model="m", input_summary={"x": 1}, output_summary={"y": 2})
        facade.record_tool_call(
            tool_name="read",
            input_summary={"path": "a.py"},
            output_summary={"status": "completed"},
            status="completed",
        )

    def test_get_observability_singleton_respects_env(self) -> None:
        from unittest.mock import patch

        with patch.dict(
            "os.environ",
            {
                "BRIDLE_OBSERVABILITY_ENABLED": "0",
            },
            clear=False,
        ):
            obs = get_observability()
            assert isinstance(obs.adapter, NoopObservabilityAdapter)

    def test_context_binding(self) -> None:
        facade = ObservabilityFacade(ObservabilityConfig.disabled())
        with facade.bind_context(ObservabilityContext(session_id="s1", run_id="r1")):
            ctx = facade.current_context()
            assert ctx.session_id == "s1"
            assert ctx.run_id == "r1"


class TestBuildAdapterFailFast:
    def _langfuse_config(self) -> ObservabilityConfig:
        return ObservabilityConfig(
            enabled=True,
            provider="langfuse",
            langfuse_public_key="pk",
            langfuse_secret_key="sk",
            langfuse_host="https://langfuse.example",
        )

    def test_missing_sdk_method_falls_back_to_noop(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        def _broken(**kwargs: object) -> FakeLangfuse:
            return FakeLangfuse(omit_methods=frozenset({"flush"}), **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr("bridle.observability.langfuse_adapter.Langfuse", _broken)
        with caplog.at_level(logging.WARNING):
            adapter = _build_adapter(self._langfuse_config())

        assert isinstance(adapter, NoopObservabilityAdapter)
        assert any("observability_langfuse_init_failed" in r.message for r in caplog.records)

    def test_incompatible_sdk_no_runtime_failed_logs(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        def _broken(**kwargs: object) -> FakeLangfuse:
            return FakeLangfuse(omit_observation_methods=frozenset({"update"}), **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr("bridle.observability.langfuse_adapter.Langfuse", _broken)
        with caplog.at_level(logging.WARNING):
            adapter = _build_adapter(self._langfuse_config())

        assert isinstance(adapter, NoopObservabilityAdapter)
        facade = ObservabilityFacade(self._langfuse_config(), adapter=adapter)
        with caplog.at_level(logging.ERROR):
            facade.start_trace("node_agent.run")
            facade.record_generation(model="m", input_summary={"x": 1}, output_summary={"y": 2})
        assert not any("observability_start_trace_failed" in r.message for r in caplog.records)
        assert not any("observability_record_generation_failed" in r.message for r in caplog.records)

    def test_compatible_sdk_logs_ready(self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
        monkeypatch.setattr("bridle.observability.langfuse_adapter.Langfuse", FakeLangfuse)
        with caplog.at_level(logging.INFO):
            adapter = _build_adapter(self._langfuse_config())

        assert not isinstance(adapter, NoopObservabilityAdapter)
        assert any("observability_langfuse_ready" in r.message for r in caplog.records)

    def test_init_does_not_open_network_socket(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _forbidden(*args: object, **kwargs: object) -> None:
            raise AssertionError("socket used during Langfuse adapter init")

        monkeypatch.setattr("socket.socket", _forbidden)
        monkeypatch.setattr("bridle.observability.langfuse_adapter.Langfuse", FakeLangfuse)
        adapter = _build_adapter(self._langfuse_config())
        assert not isinstance(adapter, NoopObservabilityAdapter)
