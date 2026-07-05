"""Tests for `bridle obs check` CLI."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from bridle.cli import app

runner = CliRunner()


def _langfuse_env() -> dict[str, str]:
    return {
        "BRIDLE_OBSERVABILITY_ENABLED": "1",
        "LANGFUSE_PUBLIC_KEY": "pk-test",
        "LANGFUSE_SECRET_KEY": "sk-test",
        "LANGFUSE_HOST": "https://cloud.langfuse.com",
    }


class TestObsCheckCli:
    def test_success_path_prints_trace_id_and_exits_zero(self) -> None:
        mock_trace = MagicMock()
        mock_trace.trace_id = "trace-123"
        mock_adapter = MagicMock()
        mock_adapter._client = MagicMock()
        mock_adapter._client.get_trace_url.return_value = "https://cloud.langfuse.com/trace/trace-123"

        with (
            patch.dict("os.environ", _langfuse_env(), clear=False),
            patch("bridle.observability.facade.ObservabilityFacade") as mock_facade_cls,
        ):
                mock_facade = MagicMock()
                mock_facade.adapter = mock_adapter
                mock_facade.start_trace.return_value = mock_trace
                mock_facade_cls.return_value = mock_facade
                with patch("langfuse.__version__", "4.7.1"):
                    result = runner.invoke(app, ["obs", "check"])

        assert result.exit_code == 0, result.output
        assert "provider=langfuse" in result.output
        assert "trace-123" in result.output
        assert "https://cloud.langfuse.com/trace/trace-123" in result.output
        mock_facade.flush.assert_called_once()

    def test_noop_provider_exits_nonzero(self) -> None:
        with (
            patch.dict("os.environ", {"BRIDLE_OBSERVABILITY_ENABLED": "0"}, clear=False),
            patch("bridle.observability.facade.ObservabilityFacade") as mock_facade_cls,
        ):
                from bridle.observability.noop_adapter import NoopObservabilityAdapter

                mock_facade = MagicMock()
                mock_facade.adapter = NoopObservabilityAdapter()
                mock_facade_cls.return_value = mock_facade
                with patch("langfuse.__version__", "4.7.1"):
                    result = runner.invoke(app, ["obs", "check"])

        assert result.exit_code != 0
        assert "provider=noop" in result.output

    def test_missing_client_method_exits_nonzero(self) -> None:
        mock_client = MagicMock()
        del mock_client.start_observation
        mock_adapter = MagicMock()
        mock_adapter._client = mock_client

        with (
            patch.dict("os.environ", _langfuse_env(), clear=False),
            patch("bridle.observability.facade.ObservabilityFacade") as mock_facade_cls,
        ):
                mock_facade = MagicMock()
                mock_facade.adapter = mock_adapter
                mock_facade_cls.return_value = mock_facade
                with patch("langfuse.__version__", "4.7.1"):
                    result = runner.invoke(app, ["obs", "check"])

        assert result.exit_code != 0
        assert "start_observation" in result.output
