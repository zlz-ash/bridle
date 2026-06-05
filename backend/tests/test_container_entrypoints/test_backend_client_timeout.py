"""BridleBackendClient negotiate_complexity uses extended timeout."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from bridle.container_entrypoints.backend_client import BridleBackendClient


def test_negotiate_complexity_uses_180s_timeout() -> None:
    client = BridleBackendClient("http://localhost:8000", timeout_seconds=30.0)
    with patch("httpx.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client_cls.return_value.__enter__.return_value = mock_client
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"renegotiated": True}
        mock_client.post.return_value = mock_response

        client.negotiate_complexity("plan-1")

    mock_client_cls.assert_called_once()
    assert mock_client_cls.call_args.kwargs["timeout"] == 180.0


def test_select_node_uses_default_30s_timeout() -> None:
    client = BridleBackendClient("http://localhost:8000", timeout_seconds=30.0)
    with patch("httpx.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client_cls.return_value.__enter__.return_value = mock_client
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"ok": True}
        mock_client.post.return_value = mock_response

        client.select_node("session-1", "n1")

    mock_client_cls.assert_called_once()
    assert mock_client_cls.call_args.kwargs["timeout"] == 30.0
