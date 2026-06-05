"""Tests for container agent environment injection."""
from __future__ import annotations

import pytest

from bridle.services.container_agent_env import build_agent_container_env


class TestContainerAgentEnv:
    def test_default_proxy_points_at_host_gateway(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("HTTP_PROXY", raising=False)
        monkeypatch.delenv("HTTPS_PROXY", raising=False)
        env = build_agent_container_env()
        assert env["HTTP_PROXY"] == "http://host.docker.internal:7890"
        assert env["HTTPS_PROXY"] == "http://host.docker.internal:7890"

    def test_localhost_proxy_rewritten_for_container(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:7890")
        monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7890")
        env = build_agent_container_env()
        assert env["HTTP_PROXY"] == "http://host.docker.internal:7890"
        assert env["HTTPS_PROXY"] == "http://host.docker.internal:7890"

    def test_third_party_proxy_passed_through(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HTTP_PROXY", "http://my-corp-proxy:8080")
        monkeypatch.setenv("HTTPS_PROXY", "http://my-corp-proxy:8080")
        env = build_agent_container_env()
        assert env["HTTP_PROXY"] == "http://my-corp-proxy:8080"
        assert env["HTTPS_PROXY"] == "http://my-corp-proxy:8080"

    def test_run_and_node_ids_only_when_provided(self) -> None:
        base = build_agent_container_env()
        assert "BRIDLE_RUN_ID" not in base
        assert "BRIDLE_NODE_ID" not in base
        with_ids = build_agent_container_env(run_id="r1", node_id="n1")
        assert with_ids["BRIDLE_RUN_ID"] == "r1"
        assert with_ids["BRIDLE_NODE_ID"] == "n1"

    def test_no_proxy_includes_host_gateway(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("NO_PROXY", raising=False)
        monkeypatch.delenv("no_proxy", raising=False)
        env = build_agent_container_env()
        for host in ("host.docker.internal", "localhost", "127.0.0.1"):
            assert host in env["NO_PROXY"]
            assert host in env["no_proxy"]

    def test_no_proxy_preserves_user_entries(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NO_PROXY", "internal.corp,10.0.0.0/8")
        env = build_agent_container_env()
        assert env["NO_PROXY"].startswith("internal.corp,10.0.0.0/8")
        assert "host.docker.internal" in env["NO_PROXY"]
