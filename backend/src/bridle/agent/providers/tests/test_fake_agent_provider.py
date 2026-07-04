"""Tests for FakeAgentProvider -structured output, no file/network access."""
from __future__ import annotations

from pathlib import Path

import pytest

from bridle.agent.runtime.schemas import AgentContext


class TestFakeAgentProvider:
    """Unit tests for fake agent provider."""

    @pytest.mark.asyncio
    async def test_generate_returns_structured_proposal(self) -> None:
        from bridle.agent.providers.fake_agent_provider import FakeAgentProvider

        provider = FakeAgentProvider()
        ctx = AgentContext(
            instruction="Implement code_change node n2",
            node={"id": "n2"},
            allowed_files=["src/example.py"],
            accessible_context={
                "node_id": "n2",
                "accessible": [
                    {
                        "node_id": "n1",
                        "interface_name": "auth_context",
                        "fields": [{"name": "user_id", "type": "string"}],
                        "endpoints": [{"name": "get_user", "method": "GET", "path": "/users/me"}],
                    }
                ],
            },
        )
        result = await provider.generate(ctx)
        assert result.summary != ""
        assert isinstance(result.file_patches, list)
        assert isinstance(result.tests_to_run, list)

    @pytest.mark.asyncio
    async def test_generate_file_patches_have_required_shape(self) -> None:
        from bridle.agent.providers.fake_agent_provider import FakeAgentProvider

        provider = FakeAgentProvider()
        ctx = AgentContext(
            instruction="Do something",
            node={"id": "n1"},
            allowed_files=["src/a.py", "src/b.py"],
            accessible_context={},
        )
        result = await provider.generate(ctx)

        for patch in result.file_patches:
            assert patch.path != ""
            assert patch.change_type in ("modify", "add", "remove")

    @pytest.mark.asyncio
    async def test_generate_respects_allowed_files(self) -> None:
        from bridle.agent.providers.fake_agent_provider import FakeAgentProvider

        provider = FakeAgentProvider()
        ctx = AgentContext(
            instruction="Only touch allowed files",
            node={"id": "n1"},
            allowed_files=["src/a.py"],
            accessible_context={},
        )
        result = await provider.generate(ctx)

        for patch in result.file_patches:
            assert patch.path in ctx.allowed_files, (
                f"Fake provider returned patch for '{patch.path}' "
                f"which is not in allowed_files"
            )

    @pytest.mark.asyncio
    async def test_generate_empty_allowed_files_yields_empty_patches(self) -> None:
        from bridle.agent.providers.fake_agent_provider import FakeAgentProvider

        provider = FakeAgentProvider()
        ctx = AgentContext(
            instruction="No files allowed",
            node={"id": "n1"},
            allowed_files=[],
            accessible_context={},
        )
        result = await provider.generate(ctx)
        assert result.file_patches == []

    @pytest.mark.asyncio
    async def test_generate_always_returns_different_summary_per_instruction(self) -> None:
        from bridle.agent.providers.fake_agent_provider import FakeAgentProvider

        provider = FakeAgentProvider()
        r1 = await provider.generate(AgentContext(
            instruction="Implement auth",
            node={"id": "n1"},
            allowed_files=["src/auth.py"],
            accessible_context={},
        ))
        r2 = await provider.generate(AgentContext(
            instruction="Write tests",
            node={"id": "n1"},
            allowed_files=["tests/test_auth.py"],
            accessible_context={},
        ))
        assert r1.summary != r2.summary

    @pytest.mark.asyncio
    async def test_generate_does_not_read_files(self, test_workspace: Path) -> None:
        """Provider must not read files from disk.

        Even if a real file with sensitive content exists, the provider
        only uses path strings to build deterministic diffs -it never reads.
        """
        from bridle.agent.providers.fake_agent_provider import FakeAgentProvider

        provider = FakeAgentProvider()
        # Write a real file with secret content
        f = test_workspace / "provider_read_guard.py"
        f.write_text("SECRET DATA")

        # Provider only sees a POSIX relative path, never the real file content
        ctx = AgentContext(
            instruction="Do something",
            node={"id": "n1"},
            allowed_files=["provider_read_guard.py"],
            accessible_context={},
        )
        result = await provider.generate(ctx)
        for patch in result.file_patches:
            assert "SECRET DATA" not in patch.diff

    @pytest.mark.asyncio
    async def test_generate_idempotent_same_input_same_output(self) -> None:
        from bridle.agent.providers.fake_agent_provider import FakeAgentProvider

        provider = FakeAgentProvider()
        ctx = AgentContext(
            instruction="Stable test",
            node={"id": "n1"},
            allowed_files=["a.py"],
            accessible_context={},
        )
        r1 = await provider.generate(ctx)
        r2 = await provider.generate(ctx)
        assert r1.model_dump() == r2.model_dump()

